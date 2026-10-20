#!/usr/bin/env python3
"""
Mirror every Library of Congress Subject Heading (LCSH) from id.loc.gov and write
a release: MARC (.mrc), MARCXML (.mrcxml), a manifest and checksums.

    python harvest.py            # census + fetch (resumable) + build
    python harvest.py census     # only refresh the list of headings
    python harvest.py fetch      # only fetch what's new or changed
    python harvest.py build      # only build the release from what's stored

How it works:
  * census: LC's weekly bulk file (subjects.madsrdf.jsonld.gz) lists every heading
    and when it last changed. That's the list of what should exist.
  * fetch: each heading that's new, or changed since we fetched it, is downloaded
    as MARCXML from id.loc.gov/authorities/subjects/<id>.marcxml.xml, one request
    every DELAY seconds (id.loc.gov's robots.txt asks for Crawl-delay: 3). The
    first run fetches everything (~16 days); later runs only what changed.
  * "slow down" answers (403/429) stop the run. Server hiccups (5xx, timeouts)
    pause 5 minutes and retry, at most RETRIES times, then stop. Rerun to resume.

Everything is kept in data/store.db (SQLite), so a stop never loses work.
"""
import argparse
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request

import pymarc

DUMP = "https://id.loc.gov/download/authorities/subjects.madsrdf.jsonld.gz"
RECORD = "https://id.loc.gov/authorities/subjects/{}.marcxml.xml"
DELAY = 3.1          # seconds between requests (robots.txt: Crawl-delay: 3)
PAUSE = 300          # after a server hiccup
RETRIES = 3
TIMEOUT = 60
UA = "lcsh-mirror/1.0 (+https://bibliotecaalonsocossio.com/lcshmirror/)"
ID = re.compile(r"/authorities/subjects/(sh\d+)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS census(id TEXT PRIMARY KEY, changed TEXT);
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, xml BLOB, changed TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS gone(id TEXT PRIMARY KEY, reason TEXT, changed TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Stop(Exception):
    """Something needs a human; stop and say why."""


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(data):
    os.makedirs(data, exist_ok=True)
    c = sqlite3.connect(os.path.join(data, "store.db"), isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


# ---------------------------------------------------------------- census

def download(url, path, opener=urllib.request.urlopen):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener(req, timeout=600) as r, open(path + ".part", "wb") as fh:
        while chunk := r.read(1 << 20):
            fh.write(chunk)
    os.replace(path + ".part", path)


def read_dump(path):
    """(id, last change date) for every current heading in the bulk file."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            graph = json.loads(line).get("@graph", [])
            for node in graph:
                m = ID.search(node.get("@id", ""))
                types = node.get("@type", [])
                if m and "madsrdf:Authority" in (types if isinstance(types, list) else [types]):
                    dates = [n["ri:recordChangeDate"]["@value"] for n in graph
                             if isinstance(n.get("ri:recordChangeDate"), dict)]
                    yield m.group(1), max(dates, default="")


def census(c, data, fetch_dump=download):
    """Refresh the list of headings from LC's bulk file (downloaded at most once a week)."""
    path = os.path.join(data, "subjects.madsrdf.jsonld.gz")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 6 * 86400:
        fetch_dump(DUMP, path)
    rows = list(read_dump(path))
    if len(rows) < 1000:
        raise Stop(f"bulk file lists only {len(rows)} headings; refusing to use it")
    c.execute("BEGIN")
    c.execute("DELETE FROM census")
    c.executemany("INSERT OR REPLACE INTO census VALUES(?,?)", rows)
    c.execute("COMMIT")
    return len(rows)


# ---------------------------------------------------------------- fetch

def get(id_, opener=urllib.request.urlopen):
    """(status, body) for one record. Network errors come back as status 0."""
    req = urllib.request.Request(RECORD.format(id_), headers={"User-Agent": UA})
    try:
        with opener(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return 0, repr(e).encode()


def todo(c):
    """Headings that are new, or changed at LC since we fetched them."""
    return [r[0] for r in c.execute(
        "SELECT census.id FROM census LEFT JOIN records USING(id) LEFT JOIN gone g USING(id) "
        "WHERE (records.id IS NULL OR records.changed < census.changed) "
        "  AND (g.id IS NULL OR g.changed < census.changed) ORDER BY census.id")]


def heading_of(xml):
    m = re.search(rb'tag="1\d\d"[^>]*>(.*?)</(?:marcxml:)?datafield>', xml, re.S)
    return " ".join(x.decode("utf-8", "replace") for x in re.findall(rb'code="[avxyz]">([^<]*)<', m.group(1))) if m else ""


class Progress:
    """Writes data/dash/status.json for the dashboard and the health check."""

    def __init__(self, data, total):
        self.path = os.path.join(data, "dash", "status.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        if os.path.exists(page):
            shutil.copyfile(page, os.path.join(data, "dash", "index.html"))
        self.s = {"state": "fetching", "started": now(), "total": total, "done": 0,
                  "errors": [], "last": None, "updated": now()}
        self.t0 = time.time()

    def write(self, **kw):
        self.s.update(kw, updated=now())
        el = time.time() - self.t0
        if self.s["done"] and el > 0:
            rate = self.s["done"] / el
            self.s["per_hour"] = round(rate * 3600)
            left = (self.s["total"] - self.s["done"]) / rate
            self.s["eta"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=left)).strftime("%Y-%m-%dT%H:%MZ")
        with open(self.path + ".part", "w", encoding="utf-8") as fh:
            json.dump(self.s, fh)
        os.replace(self.path + ".part", self.path)

    def error(self, msg):
        self.s["errors"] = (self.s["errors"] + [f"{now()} {msg}"])[-10:]


def fetch(c, data, getter=get, sleep=time.sleep):
    ids = todo(c)
    changed = dict(c.execute("SELECT id, changed FROM census"))
    p = Progress(data, len(ids))
    p.write()
    last = 0.0
    for i, id_ in enumerate(ids):
        for attempt in range(RETRIES + 1):
            wait = DELAY - (time.monotonic() - last)
            if wait > 0:
                sleep(wait)
            last = time.monotonic()
            status, body = getter(id_)
            if status == 200 or status in (403, 404, 410, 429):
                break
            p.error(f"{id_}: {'network ' + body.decode()[:80] if status == 0 else f'HTTP {status}'}")
            if attempt == RETRIES:
                p.write(state="stopped")
                raise Stop(f"{id_}: still failing after {RETRIES} retries (HTTP {status})")
            p.write(state=f"paused after HTTP {status}, retry {attempt + 1}/{RETRIES}")
            sleep(PAUSE)
        if status in (403, 429):
            p.error(f"{id_}: HTTP {status}: LC asks us to slow down")
            p.write(state="stopped")
            raise Stop(f"{id_}: HTTP {status}, LC asked us to slow down")
        if status == 200 and b"datafield" in body and b"<html" not in body[:500].lower():
            c.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?)", (id_, body, changed[id_], now()))
            c.execute("DELETE FROM gone WHERE id=?", (id_,))
            last_rec = {"id": id_, "heading": heading_of(body), "at": now()}
        else:
            c.execute("INSERT OR REPLACE INTO gone VALUES(?,?,?)",
                      (id_, f"HTTP {status}" if status != 200 else "not a MARC record", changed[id_]))
            last_rec = {"id": id_, "heading": f"(skipped: HTTP {status})", "at": now()}
        if i % 10 == 0 or i == len(ids) - 1:
            p.write(state="fetching", done=i + 1, last=last_rec)
    p.write(state="fetched", done=len(ids))
    return len(ids)


# ---------------------------------------------------------------- build

def lccn(rec):
    f = rec["010"]
    return (f.get("a") or "").strip() if f else ""


def heading(rec):
    f = next((f for f in rec.get_fields() if f.tag.startswith("1")), None)
    return "--".join(sf.value.strip() for sf in f.subfields if sf.code in "avxyz") if f else ""


def build(c, out, date=None):
    """Write a release from every stored heading in the current census.
    Complete when every heading in the census is either stored and up to date, or gone."""
    if todo(c):
        print(f"INCOMPLETE: {len(todo(c))} headings not fetched yet")
        return False
    date = date or dt.date.today().strftime("%Y%m%d")
    os.makedirs(out, exist_ok=True)
    base = os.path.join(out, f"lcsh-{date}")
    rows = []
    with open(base + ".mrc", "wb") as mrc, open(base + ".mrcxml", "wb") as xml:
        xw = pymarc.XMLWriter(xml)
        for (body,) in c.execute("SELECT xml FROM records JOIN census USING(id) ORDER BY id"):
            rec = pymarc.parse_xml_to_array(io.BytesIO(body))[0]
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
    gone = c.execute("SELECT count(*) FROM gone JOIN census USING(id)").fetchone()[0]
    print(f"{len(rows)} LCSH records written to {base}.*  ({gone} listed by LC but not served as MARC)")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", nargs="?", choices=["census", "fetch", "build", "all"], default="all")
    ap.add_argument("--data", default="data", help="working folder (default: ./data)")
    a = ap.parse_args()
    c = connect(a.data)
    try:
        if a.step in ("census", "all"):
            print(f"census: {census(c, a.data)} headings")
        if a.step in ("fetch", "all"):
            print(f"fetched {fetch(c, a.data)} new or changed headings")
        if a.step in ("build", "all"):
            sys.exit(0 if build(c, os.path.join(a.data, "release")) else 2)
    except Stop as e:
        sys.exit(f"stopped: {e}\nrerun later to resume.")


if __name__ == "__main__":
    main()
