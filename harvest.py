#!/usr/bin/env python3
"""
Harvest every Library of Congress Subject Heading (LCSH) from LC's public SRU
server and write a release: MARC (.mrc), MARCXML (.mrcxml), a manifest and checksums.

    python harvest.py            # fetch (resumable) + build
    python harvest.py fetch      # only download pages
    python harvest.py build      # only build the release from downloaded pages

Gentle by design, so that LC never spends more than a tenth of its time on us:
  * one request at a time; after each answer it waits FACTOR (10) times as long
    as the server took, never less than MIN_WAIT. If LC slows down, so do we.
  * every request stays shallow: the LCCN space is first split into chunks of at
    most CHUNK records (bath.lccn="sh 850*", ...), so no request ever asks for
    "record 200,000", which gets more expensive for the server the deeper it goes.
  * any error, or any answer slower than SLOW seconds, stops the run. Nothing is
    retried. Rerun later and it resumes where it stopped.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import pymarc

SRU = "http://lx2.loc.gov:210/SAF"   # LC Subject Authority File (also holds LCGFT, children's headings)
PAGE = 200                            # records per request
CHUNK = 2000                          # most records per chunk, keeps paging shallow
FACTOR = 10                           # wait FACTOR x server time after each request
MIN_WAIT = 5.0                        # ...but never less than this
SLOW = 30.0                           # an answer slower than this stops the run
TIMEOUT = 60
UA = "lcsh-mirror/1.0 (+https://bibliotecaalonsocossio.com/lcshmirror/)"

NS = {"zs": "http://www.loc.gov/zing/srw/", "m": "http://www.loc.gov/MARC21/slim"}
MARC_NS = "{http://www.loc.gov/MARC21/slim}"


class Stop(Exception):
    """The server said something we don't like; stop and let a human look."""


# ---------------------------------------------------------------- fetch

def sru(prefix, start, maximum):
    """One searchRetrieve for LCCNs starting with prefix. Returns (raw, total, n, seconds)."""
    data = urllib.parse.urlencode({
        "version": "1.1", "operation": "searchRetrieve", "query": f'bath.lccn="{prefix}*"',
        "startRecord": start, "maximumRecords": maximum, "recordSchema": "marcxml",
    }).encode()
    req = urllib.request.Request(SRU, data=data, headers={"User-Agent": UA})
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except Exception as e:
        raise Stop(f"{prefix!r} at {start} failed: {e!r}")
    took = time.monotonic() - t
    return (raw, *parse_page(raw, f"{prefix!r} at {start}"), took)


def parse_page(raw, where):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise Stop(f"{where}: not XML ({e})")
    diag = [el.text for el in root.iter() if el.tag.endswith("message") and el.text]
    if diag:
        raise Stop(f"{where}: SRU diagnostic: {'; '.join(diag)}")
    total = root.findtext("zs:numberOfRecords", namespaces=NS)
    if total is None:
        raise Stop(f"{where}: no numberOfRecords")
    return int(total), len(list(root.iter(MARC_NS + "record")))


class Gentle:
    """Calls the server and paces itself: waits FACTOR x the time each answer took."""

    def __init__(self, call=sru, sleep=time.sleep):
        self.call, self.sleep = call, sleep

    def __call__(self, prefix, start, maximum):
        raw, total, n, took = self.call(prefix, start, maximum)
        if took > SLOW:
            raise Stop(f"{prefix!r} at {start}: server took {took:.0f}s, backing off")
        self.sleep(max(MIN_WAIT, FACTOR * took))
        return raw, total, n, took


def plan(pages, ask):
    """Split the sh LCCN space into chunks of at most CHUNK records.
    Counts are cached in pages/counts.json so a restart doesn't repeat them."""
    cache = os.path.join(pages, "counts.json")
    counts = {}
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            counts = json.load(fh)
    chunks, todo = [], [f"sh {d}" for d in "0123456789"] + [f"sh{d}" for d in "0123456789"]
    while todo:
        p = todo.pop(0)
        if p not in counts:
            counts[p] = ask(p, 1, 1)[1]
            with open(cache + ".part", "w", encoding="utf-8") as fh:
                json.dump(counts, fh)
            os.replace(cache + ".part", cache)
            print(f"count {p!r}: {counts[p]}", flush=True)
        if counts[p] == 0:
            continue
        if counts[p] <= CHUNK or len(p) >= 12:
            chunks.append(p)
        else:
            todo[:0] = [p + d for d in "0123456789"]
    return chunks


def page_path(pages, prefix, start):
    return os.path.join(pages, f"{prefix.replace(' ', '_')}-{start:07d}.xml")


def fetch(pages, ask=None):
    """Plan the chunks, then download every page that isn't on disk yet."""
    os.makedirs(pages, exist_ok=True)
    ask = ask or Gentle()
    for prefix in plan(pages, ask):
        start, total = 1, None
        while total is None or start <= total:
            path = page_path(pages, prefix, start)
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    total, _ = parse_page(fh.read(), path)
            else:
                raw, total, n, took = ask(prefix, start, PAGE)
                expected = min(PAGE, total - start + 1)
                if n != expected:
                    raise Stop(f"{prefix!r} at {start}: got {n} records, expected {expected}")
                with open(path + ".part", "wb") as fh:
                    fh.write(raw)
                os.replace(path + ".part", path)
                print(f"{prefix!r} {start + n - 1}/{total}  ({took:.1f}s)", flush=True)
            start += PAGE


# ---------------------------------------------------------------- build

def records_in(path):
    """MARC records in one saved SRU page."""
    root = ET.parse(path).getroot()
    for el in root.iter(MARC_NS + "record"):
        yield pymarc.parse_xml_to_array(io.BytesIO(ET.tostring(el)))[0]


def lccn(rec):
    f = rec["010"]
    return (f.get("a") or "").strip() if f else ""


def heading(rec):
    f = next((f for f in rec.get_fields() if f.tag.startswith("1")), None)
    return "--".join(sf.value.strip() for sf in f.subfields if sf.code in "avxyz") if f else ""


def build(pages, out, date=None):
    """Turn downloaded pages into a release. Keeps LCSH only (010 $a sh...), one copy per 001."""
    files = sorted(f for f in os.listdir(pages) if re.fullmatch(r"sh[_0-9]+-\d{7}\.xml", f))
    if not files:
        raise Stop("no pages to build from; run fetch first")
    # One completeness check per chunk: every record SRU announced arrived, and the
    # chunk didn't change size while we paged through it.
    chunks = {}
    for f in files:
        chunks.setdefault(f.rsplit("-", 1)[0], []).append(f)
    expected, complete = 0, True
    for fs in chunks.values():
        totals = []
        for f in (fs[0], fs[-1]):
            with open(os.path.join(pages, f), "rb") as fh:
                totals.append(parse_page(fh.read(), f)[0])
        if totals[0] != totals[1] or len(fs) != max(1, -(-totals[0] // PAGE)):
            complete = False
        expected += totals[0]
    date = date or dt.date.today().strftime("%Y%m%d")
    os.makedirs(out, exist_ok=True)
    base = os.path.join(out, f"lcsh-{date}")
    seen, rows, read = set(), [], 0
    with open(base + ".mrc", "wb") as mrc, open(base + ".mrcxml", "wb") as xml:
        xw = pymarc.XMLWriter(xml)
        for name in files:
            for rec in records_in(os.path.join(pages, name)):
                read += 1
                key = (rec["001"].data if rec["001"] else "").strip()
                if not lccn(rec).startswith("sh") or key in seen:
                    continue
                seen.add(key)
                mrc.write(rec.as_marc())
                xw.write(rec)
                rows.append((lccn(rec), rec["005"].data if rec["005"] else "", heading(rec)))
        xw.close(close_fh=False)
    rows.sort()
    with open(os.path.join(out, f"manifest-{date}.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["lccn", "005", "heading"])
        w.writerows(rows)
    names = [f"lcsh-{date}.mrc", f"lcsh-{date}.mrcxml", f"manifest-{date}.csv"]
    with open(os.path.join(out, f"SHA256SUMS-{date}"), "w", encoding="utf-8") as fh:
        for n in names:
            h = hashlib.sha256()
            with open(os.path.join(out, n), "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            fh.write(f"{h.hexdigest()}  {n}\n")
    ok = complete and read == expected
    print(f"{len(rows)} LCSH records written to {base}.*  "
          f"(read {read} of {expected} from SRU{'' if ok else '  INCOMPLETE: a chunk changed or pages missing, rerun'})")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["fetch", "build", "all"], default="all")
    ap.add_argument("--data", default="data", help="working folder (default: ./data)")
    a = ap.parse_args()
    pages, out = os.path.join(a.data, "pages"), os.path.join(a.data, "release")
    try:
        if a.step in ("fetch", "all"):
            fetch(pages)
        if a.step in ("build", "all"):
            if not build(pages, out):
                sys.exit(2)
            shutil.rmtree(pages)   # release is complete; next run starts fresh
    except Stop as e:
        sys.exit(f"stopped: {e}\nrerun later to resume.")


if __name__ == "__main__":
    main()
