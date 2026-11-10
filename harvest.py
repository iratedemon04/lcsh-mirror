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
import collections
import contextlib
import csv
import datetime as dt
import email.utils
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
    """Download the bulk file; remember the date LC published it (Last-Modified) in <path>.date."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener(req, timeout=600) as r, open(path + ".part", "wb") as fh:
        stamp = r.headers.get("Last-Modified")
        while chunk := r.read(1 << 20):
            fh.write(chunk)
    os.replace(path + ".part", path)
    when = email.utils.parsedate_to_datetime(stamp).date() if stamp else dt.date.today()
    with open(path + ".date", "w", encoding="utf-8") as fh:
        fh.write(when.isoformat())


def record_history(data, list_date, headings, todo_count):
    """One line per LC bulk list in data/history.csv: list date, headings, net change since the
    previous list, and how many headings were new or changed. The download page's counter
    averages these, so it gets more accurate every week."""
    path = os.path.join(data, "history.csv")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.reader(fh))[1:]
    if any(r[0] == list_date for r in rows):
        return
    net = str(headings - int(rows[-1][1])) if rows else ""
    rows.append([list_date, str(headings), net, str(todo_count)])
    with open(path + ".part", "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["list_date", "headings", "net_change", "new_or_changed"])
        w.writerows(rows)
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
    if os.path.exists(path + ".date"):
        with open(path + ".date", encoding="utf-8") as fh:
            record_history(data, fh.read().strip(), len(rows), len(todo(c)))
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


OFFLINE = (b"name resolution", b"Name or service not known", b"Network is unreachable", b"No route to host")


def offline(body):
    """True when the failure is our own connection (no DNS, no route), not LC."""
    return any(s in body for s in OFFLINE)


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

    def __init__(self, data, total, done=0):
        """total = every heading in the census; done = those already up to date (all time)."""
        self.path = os.path.join(data, "dash", "status.json")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        if os.path.exists(page):
            shutil.copyfile(page, os.path.join(data, "dash", "index.html"))
        self.s = {"state": "fetching", "started": now(), "total": total, "done": done,
                  "errors": [], "last": None, "updated": now()}
        self.recent = collections.deque(maxlen=3500)   # (time, done): ~3 h of recent pace, so a 5-min pause barely moves the ETA

    def write(self, **kw):
        self.s.update(kw, updated=now())
        if not self.recent or self.recent[-1][1] != self.s["done"]:
            self.recent.append((time.time(), self.s["done"]))
        (t0, d0), (t1, d1) = self.recent[0], self.recent[-1]
        if d1 > d0 and t1 > t0:
            rate = (d1 - d0) / (t1 - t0)
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
    total = len(changed)
    base = total - len(ids)
    p = Progress(data, total, base)
    p.write()
    last = 0.0
    for i, id_ in enumerate(ids):
        attempt, offline_since = 0, None
        while True:
            wait = DELAY - (time.monotonic() - last)
            if wait > 0:
                sleep(wait)
            last = time.monotonic()
            status, body = getter(id_)
            if status == 200 or status in (403, 404, 410, 429):
                break
            if status == 0 and offline(body):
                # our own internet/DNS is down: says nothing about LC, so just wait for it
                if not offline_since:
                    offline_since = now()
                    p.error(f"{id_}: no network ({body.decode()[:60]}), waiting")
                p.write(state=f"waiting for network since {offline_since}")
                sleep(PAUSE)
                continue
            p.error(f"{id_}: {'network ' + body.decode()[:80] if status == 0 else f'HTTP {status}'}")
            if attempt == RETRIES:
                p.write(state="stopped")
                raise Stop(f"{id_}: still failing after {RETRIES} retries (HTTP {status})")
            attempt += 1
            p.write(state=f"paused after HTTP {status}, retry {attempt}/{RETRIES}")
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
        p.write(state="fetching", done=base + i + 1, last=last_rec)
    p.write(state="fetched", done=total)
    return len(ids)


# ---------------------------------------------------------------- build

def lccn(rec):
    f = rec.get("010")
    return (f.get("a") or "").strip() if f else ""


def heading(rec):
    f = next((f for f in rec.get_fields() if f.tag.startswith("1")), None)
    return "--".join(sf.value.strip() for sf in f.subfields if sf.code in "avxyz") if f else ""


def previous_release(out, date):
    """lccn -> (sha1 of the MARC record, heading) for the newest release before `date`, or None."""
    older = sorted(f for f in os.listdir(out) if (m := re.fullmatch(r"lcsh-(\d{8})\.mrc", f)) and m.group(1) < date)
    if not older:
        return None, None
    prev = {}
    with open(os.path.join(out, older[-1]), "rb") as fh:
        for rec in pymarc.MARCReader(fh, to_unicode=True, force_utf8=True):
            prev[lccn(rec)] = (hashlib.sha1(rec.as_marc()).hexdigest(), heading(rec))
    return older[-1][5:13], prev


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def build(c, out, date=None):
    """Write a release from every stored heading in the current census.
    Complete when every heading in the census is either stored and up to date, or gone.
    When an older release exists, also write what changed since it: changes-DATE.mrc/.mrcxml/.csv
    (new or changed records; changed = different MARC content) and deleted-DATE.csv."""
    if todo(c):
        print(f"INCOMPLETE: {len(todo(c))} headings not fetched yet")
        return False
    date = date or dt.date.today().strftime("%Y%m%d")
    os.makedirs(out, exist_ok=True)
    base = os.path.join(out, f"lcsh-{date}")
    prev_date, prev = previous_release(out, date)
    names = [f"lcsh-{date}.mrc", f"lcsh-{date}.mrcxml", f"manifest-{date}.csv"]
    if prev is not None:
        names += [f"changes-{date}.mrc", f"changes-{date}.mrcxml", f"changes-{date}.csv", f"deleted-{date}.csv"]
    # Everything is written as .part and renamed once complete, so a failed build leaves
    # no truncated file for the next build to compare against or for publish to ship.
    tmp = {n: os.path.join(out, n + ".part") for n in names + [f"SHA256SUMS-{date}"]}
    rows, changes, seen = [], [], set()
    try:
        with contextlib.ExitStack() as stack:
            mrc = stack.enter_context(open(tmp[f"lcsh-{date}.mrc"], "wb"))
            xw = pymarc.XMLWriter(stack.enter_context(open(tmp[f"lcsh-{date}.mrcxml"], "wb")))
            if prev is not None:
                cmrc = stack.enter_context(open(tmp[f"changes-{date}.mrc"], "wb"))
                cxw = pymarc.XMLWriter(stack.enter_context(open(tmp[f"changes-{date}.mrcxml"], "wb")))
            for (body,) in c.execute("SELECT xml FROM records JOIN census USING(id) ORDER BY id"):
                rec = pymarc.parse_xml_to_array(io.BytesIO(body))[0]
                raw = rec.as_marc()
                mrc.write(raw)
                xw.write(rec)
                row = (lccn(rec), rec.get("005").data if rec.get("005") else "", heading(rec))
                rows.append(row)
                if prev is not None:
                    seen.add(row[0])
                    old = prev.get(row[0])
                    if old is None or old[0] != hashlib.sha1(raw).hexdigest():
                        cmrc.write(raw)
                        cxw.write(rec)
                        changes.append((row[0], "new" if old is None else "changed", row[1], row[2]))
            xw.close(close_fh=False)
            if prev is not None:
                cxw.close(close_fh=False)
        rows.sort()
        write_csv(tmp[f"manifest-{date}.csv"], ["lccn", "005", "heading"], rows)
        if prev is not None:
            changes.sort()
            deleted = sorted((k, v[1]) for k, v in prev.items() if k not in seen)
            write_csv(tmp[f"changes-{date}.csv"], ["lccn", "status", "005", "heading"], changes)
            write_csv(tmp[f"deleted-{date}.csv"], ["lccn", "heading"], deleted)
        with open(tmp[f"SHA256SUMS-{date}"], "w", encoding="utf-8") as fh:
            for n in names:
                h = hashlib.sha256()
                with open(tmp[n], "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                fh.write(f"{h.hexdigest()}  {n}\n")
    except BaseException:
        for p in tmp.values():
            if os.path.exists(p):
                os.remove(p)
        raise
    for n, p in tmp.items():
        os.replace(p, os.path.join(out, n))
    if prev is not None:
        new = sum(1 for x in changes if x[1] == "new")
        print(f"since {prev_date}: {new} new, {len(changes) - new} changed, {len(deleted)} deleted")
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
