#!/usr/bin/env python3
"""
Harvest every Library of Congress Subject Heading (LCSH) from LC's public SRU
server and write a release: MARC (.mrc), MARCXML (.mrcxml), a manifest and checksums.

    python harvest.py            # fetch (resumable) + build
    python harvest.py fetch      # only download pages
    python harvest.py build      # only build the release from downloaded pages

Polite by design: one request at a time, and after each response it waits at
least DELAY seconds, or as long as the server took to answer, whichever is longer.
Any error (HTTP, timeout, dropped connection, SRU diagnostic, short page) stops
the run. Nothing is retried automatically. Rerun later to resume from where it stopped.
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
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
QUERY = 'bath.lccn="sh*"'             # every record with an sh LCCN
PAGE = 200                            # records per request
DELAY = 3.0                           # minimum seconds between requests
TIMEOUT = 120
UA = "lcsh-mirror/1.0 (+https://bibliotecaalonsocossio.com/authorities)"

NS = {"zs": "http://www.loc.gov/zing/srw/", "m": "http://www.loc.gov/MARC21/slim"}
MARC_NS = "{http://www.loc.gov/MARC21/slim}"


class Stop(Exception):
    """The server said something we don't like; stop and let a human look."""


# ---------------------------------------------------------------- fetch

def get_page(start):
    """One searchRetrieve request. Returns (raw bytes, numberOfRecords, records on page)."""
    data = urllib.parse.urlencode({
        "version": "1.1", "operation": "searchRetrieve", "query": QUERY,
        "startRecord": start, "maximumRecords": PAGE, "recordSchema": "marcxml",
    }).encode()
    req = urllib.request.Request(SRU, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
    except Exception as e:
        raise Stop(f"request at startRecord={start} failed: {e!r}")
    return (raw, *parse_page(raw, start))


def parse_page(raw, start):
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise Stop(f"page {start}: not XML ({e})")
    diag = [el.text for el in root.iter() if el.tag.endswith("message") and el.text]
    if diag:
        raise Stop(f"page {start}: SRU diagnostic: {'; '.join(diag)}")
    total = root.findtext("zs:numberOfRecords", namespaces=NS)
    if total is None:
        raise Stop(f"page {start}: no numberOfRecords")
    return int(total), len(list(root.iter(MARC_NS + "record")))


def page_path(pages, start):
    return os.path.join(pages, f"{start:07d}.xml")


def fetch(pages, delay=DELAY, get=get_page, sleep=time.sleep):
    """Download pages that aren't on disk yet. Returns numberOfRecords."""
    os.makedirs(pages, exist_ok=True)
    start, total = 1, None
    while total is None or start <= total:
        path = page_path(pages, start)
        if os.path.exists(path):
            with open(path, "rb") as fh:
                total, _ = parse_page(fh.read(), start)
        else:
            t = time.monotonic()
            raw, total, n = get(start)
            took = time.monotonic() - t
            expected = min(PAGE, total - start + 1)
            if n != expected:
                raise Stop(f"page {start}: got {n} records, expected {expected}")
            with open(path + ".part", "wb") as fh:
                fh.write(raw)
            os.replace(path + ".part", path)
            print(f"{start + n - 1:>7}/{total}  ({took:.1f}s)", flush=True)
            sleep(max(delay, took))
        start += PAGE
    return total


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
    files = sorted(f for f in os.listdir(pages) if re.fullmatch(r"\d{7}\.xml", f))
    if not files:
        raise Stop("no pages to build from; run fetch first")
    with open(os.path.join(pages, files[0]), "rb") as fh:
        first_total, _ = parse_page(fh.read(), 1)
    with open(os.path.join(pages, files[-1]), "rb") as fh:
        last_total, _ = parse_page(fh.read(), 0)
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
    # The one completeness check: every record SRU promised arrived, and the
    # result set didn't change size while we were paging through it.
    ok = read == first_total == last_total
    print(f"{len(rows)} LCSH records written to {base}.*  "
          f"(read {read} of {first_total} from SRU{'' if ok else '  INCOMPLETE: result set changed or pages missing, rerun'})")
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["fetch", "build", "all"], default="all")
    ap.add_argument("--data", default="data", help="working folder (default: ./data)")
    ap.add_argument("--delay", type=float, default=DELAY, help="minimum seconds between requests")
    a = ap.parse_args()
    pages, out = os.path.join(a.data, "pages"), os.path.join(a.data, "release")
    try:
        if a.step in ("fetch", "all"):
            fetch(pages, a.delay)
        if a.step in ("build", "all"):
            if not build(pages, out):
                sys.exit(2)
            shutil.rmtree(pages)   # release is complete; next run starts fresh
    except Stop as e:
        sys.exit(f"stopped: {e}\nrerun later to resume.")


if __name__ == "__main__":
    main()
