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
    and when it last changed. That's the list of what should exist. The bulk file
    can be weeks behind the live site, so LC's Atom feed of recent changes
    (id.loc.gov/authorities/subjects/feed/N) adds what is newer than it.
  * fetch: each heading that's new, or changed since we fetched it, is downloaded
    as MARCXML from id.loc.gov/authorities/subjects/<id>.marcxml.xml, one request
    every DELAY seconds (id.loc.gov's robots.txt asks for Crawl-delay: 3). The
    first run fetches everything (~16 days); later runs only what changed.
  * "slow down" answers (403/429) stop the run. Server hiccups (5xx, timeouts, a
    page that is not MARC) pause 5 minutes and retry, at most RETRIES times, then
    stop. Rerun to resume. A 404 is asked again RECHECK days later.
  * the change feed is optional: if it cannot be read the run goes on with the bulk
    file and prints a WARNING. A release that would drop more than MAX_DELETED
    headings is not written.

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
FEED = "https://id.loc.gov/authorities/subjects/feed/{}"
FEED_OVERLAP = 7     # days of the feed re-read before the bulk file's newest change
FEED_PAGES = 100     # 100 headings a page; more than this means something is off
DELAY = 3.1          # seconds between requests (robots.txt: Crawl-delay: 3)
PAUSE = 300          # after a server hiccup
RETRIES = 3
TIMEOUT = 60
MAX_DELETED = 500    # a release that would drop more headings than this is not written (see build)
RECHECK = 6          # days before a heading LC answered 404 for is asked for again
UA = "lcsh-mirror/1.0 (+https://bibliotecaalonsocossio.com/lcshmirror/)"
ID = re.compile(r"/authorities/subjects/(sh\d+)$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS census(id TEXT PRIMARY KEY, changed TEXT);
CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY, xml BLOB, changed TEXT, fetched TEXT);
CREATE TABLE IF NOT EXISTS gone(id TEXT PRIMARY KEY, reason TEXT, changed TEXT, checked TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class Stop(Exception):
    """Something needs a human; stop and say why."""


class SlowDown(Stop):
    """LC answered 403 or 429: make no more requests in this run."""


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(data):
    os.makedirs(data, exist_ok=True)
    c = sqlite3.connect(os.path.join(data, "store.db"), isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    if "checked" not in [r[1] for r in c.execute("PRAGMA table_info(gone)")]:
        c.execute("ALTER TABLE gone ADD COLUMN checked TEXT")      # stores made before 2026-11-14
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


def get_url(url, opener=urllib.request.urlopen):
    """(status, body) for any id.loc.gov URL. Network errors come back as status 0."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener(req, timeout=TIMEOUT) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return 0, repr(e).encode()


ENTRY = re.compile(rb"<entry>.*?<id>info:lc/authorities/subjects/([^<]+)</id>.*?<updated>(\d{4}-\d\d-\d\d)", re.S)


def read_feed(since, getter=get_url, sleep=time.sleep):
    """(id, date) for every heading LC's Atom feed lists as changed on or after `since`
    (YYYY-MM-DD). The feed is newest first, 100 a page; read until a page reaches `since`.
    Entries that are not headings (e.g. sh...-781 subdivision forms) are skipped."""
    found = {}
    for page in range(1, FEED_PAGES + 1):
        if page > 1:
            sleep(DELAY)
        attempt = 0
        while True:
            status, body = getter(FEED.format(page))
            if status == 200:
                break
            if status in (403, 429):
                raise SlowDown(f"change feed page {page}: HTTP {status}, LC asked us to slow down")
            if status == 0 and offline(body):      # our own connection: wait for it, as fetch does
                sleep(PAUSE)
                continue
            if attempt == RETRIES:
                raise Stop(f"change feed page {page}: HTTP {status} after {RETRIES} retries")
            attempt += 1
            sleep(PAUSE)
        entries = [(i.decode(), d.decode()) for i, d in ENTRY.findall(body)]
        if not entries:
            raise Stop(f"change feed page {page} has no entries" +
                       ("; LC may have changed its format" if page == 1 else f" before reaching {since}"))
        for id_, date in entries:
            if re.fullmatch(r"sh\d+", id_) and date >= since and date > found.get(id_, ""):
                found[id_] = date
        if min(d for _, d in entries) < since:
            return sorted(found.items())
    raise Stop(f"change feed: {FEED_PAGES} pages read without reaching {since}")


def census(c, data, fetch_dump=download, feed=None):
    """Refresh the list of headings: LC's bulk file (downloaded at most once a week), plus
    whatever LC's change feed says is newer than the bulk file.

    The feed is optional. If it cannot be read, or an empty file named no-feed in the data
    directory switches it off, the run goes on with the bulk file and prints a WARNING line.
    Headings we already hold that only the feed knew about are kept either way, until the bulk
    file is newer than they are: from then on the bulk file decides whether they exist."""
    path = os.path.join(data, "subjects.madsrdf.jsonld.gz")
    if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 5 * 86400:   # a late run one week must not make the next one skip it
        fetch_dump(DUMP, path)
    rows = list(read_dump(path))
    if len(rows) < 1000:
        raise Stop(f"bulk file lists only {len(rows)} headings; refusing to use it")
    newest = max(d for _, d in rows)[:10]
    kept, newer, warning = [], [], None
    if re.fullmatch(r"\d{4}-\d\d-\d\d", newest):
        in_bulk = {i for i, _ in rows}
        kept = [r for r in c.execute("SELECT census.id, census.changed FROM census JOIN records USING(id) "
                                     "WHERE substr(census.changed,1,10) >= ?", (newest,)) if r[0] not in in_bulk]
        if os.path.exists(os.path.join(data, "no-feed")):
            warning = "change feed switched off (data/no-feed)"
        else:
            since = (dt.date.fromisoformat(newest) - dt.timedelta(days=FEED_OVERLAP)).isoformat()
            try:
                newer = (feed or read_feed)(since)
            except SlowDown:
                raise
            except Stop as e:
                warning = f"change feed not read ({e})"
    # everything was read before the table is touched, so a failure above leaves the old census whole
    c.execute("BEGIN")
    c.execute("DELETE FROM census")
    c.executemany("INSERT OR REPLACE INTO census VALUES(?,?)", rows)
    c.executemany("INSERT OR IGNORE INTO census VALUES(?,?)", kept)
    # a feed date only counts when it is a later day than the bulk file's date for that heading
    c.executemany("INSERT INTO census VALUES(?,?) ON CONFLICT(id) DO UPDATE SET changed=excluded.changed "
                  "WHERE substr(census.changed,1,10) < excluded.changed", newer)
    c.execute("COMMIT")
    if warning:
        print(f"WARNING: {warning}; nothing LC changed after {newest} is picked up this run")
    total = c.execute("SELECT count(*) FROM census").fetchone()[0]
    if os.path.exists(path + ".date"):
        with open(path + ".date", encoding="utf-8") as fh:
            record_history(data, fh.read().strip(), total, len(todo(c)))
    return total


# ---------------------------------------------------------------- fetch

def get(id_, opener=urllib.request.urlopen):
    """(status, body) for one record. Network errors come back as status 0."""
    return get_url(RECORD.format(id_), opener)


DELETED = "deleted at LC"


def deleted(xml):
    """True when LC serves the record with leader/05 = d (deleted). The bulk file drops these;
    the change feed still lists them."""
    m = re.search(rb"<(?:marcxml:)?leader[^>]*>.{5}(.)", xml)
    return bool(m) and m.group(1) == b"d"


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


def recheck(c):
    """Headings LC answered 404 for (or something that was not MARC) at least RECHECK days ago.
    Asked again, so one wrong answer from LC never drops a heading for good."""
    before = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RECHECK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [r[0] for r in c.execute(
        "SELECT g.id FROM gone g JOIN census USING(id) WHERE (g.reason LIKE 'HTTP 4%' OR g.reason = 'not a MARC record') "
        "AND (g.checked IS NULL OR g.checked < ?) ORDER BY g.id", (before,))]


def is_marc(xml):
    return b"datafield" in xml and b"<html" not in xml[:500].lower()


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
    work = ids + [i for i in recheck(c) if i not in set(ids)]
    changed = dict(c.execute("SELECT id, changed FROM census"))
    total = len(changed)
    base = total - len(work)
    p = Progress(data, total, base)
    p.write()
    last = 0.0
    for i, id_ in enumerate(work):
        attempt, offline_since = 0, None
        while True:
            wait = DELAY - (time.monotonic() - last)
            if wait > 0:
                sleep(wait)
            last = time.monotonic()
            status, body = getter(id_)
            if status in (403, 404, 410, 429) or (status == 200 and (deleted(body) or is_marc(body))):
                break
            if status == 0 and offline(body):
                # our own internet/DNS is down: says nothing about LC, so just wait for it
                if not offline_since:
                    offline_since = now()
                    p.error(f"{id_}: no network ({body.decode()[:60]}), waiting")
                p.write(state=f"waiting for network since {offline_since}")
                sleep(PAUSE)
                continue
            # a 200 that is not a MARC record (an error page, say) is LC having a bad moment, not an answer
            what = "HTTP 200 but not a MARC record" if status == 200 else f"HTTP {status}"
            p.error(f"{id_}: {'network ' + body.decode()[:80] if status == 0 else what}")
            if attempt == RETRIES:
                p.write(state="stopped")
                raise Stop(f"{id_}: still failing after {RETRIES} retries ({what})")
            attempt += 1
            p.write(state=f"paused after {what}, retry {attempt}/{RETRIES}")
            sleep(PAUSE)
        if status in (403, 429):
            p.error(f"{id_}: HTTP {status}: LC asks us to slow down")
            p.write(state="stopped")
            raise SlowDown(f"{id_}: HTTP {status}, LC asked us to slow down")
        if status == 200 and deleted(body):
            c.execute("INSERT OR REPLACE INTO gone VALUES(?,?,?,?)", (id_, DELETED, changed[id_], now()))
            c.execute("DELETE FROM records WHERE id=?", (id_,))
            last_rec = {"id": id_, "heading": f"(deleted at LC: {heading_of(body)})", "at": now()}
        elif status == 200:
            c.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?)", (id_, body, changed[id_], now()))
            c.execute("DELETE FROM gone WHERE id=?", (id_,))
            last_rec = {"id": id_, "heading": heading_of(body), "at": now()}
        else:
            c.execute("INSERT OR REPLACE INTO gone VALUES(?,?,?,?)", (id_, f"HTTP {status}", changed[id_], now()))
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


def build(c, out, date=None, allow_deletions=False):
    """Write a release from every stored heading in the current census.
    Complete when every heading in the census is either stored and up to date, or gone.
    When an older release exists, also write what changed since it: changes-DATE.mrc/.mrcxml/.csv
    (new or changed records; changed = different MARC content) and deleted-DATE.csv.
    A release that would drop more than MAX_DELETED headings is not written, unless
    allow_deletions: LC removes a handful a week, so thousands means a bad bulk file."""
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
            if len(deleted) > MAX_DELETED and not allow_deletions:
                raise Stop(f"{len(deleted)} headings would be deleted since {prev_date} (limit {MAX_DELETED}); "
                           "no release written. If LC really removed them: touch data/allow-deletions and run again")
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
    gone = c.execute("SELECT count(*) FROM gone JOIN census USING(id) WHERE reason != ?", (DELETED,)).fetchone()[0]
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
            flag = os.path.join(a.data, "allow-deletions")      # one-time OK for a release that shrinks a lot
            if not build(c, os.path.join(a.data, "release"), allow_deletions=os.path.exists(flag)):
                sys.exit(2)
            if os.path.exists(flag):
                os.remove(flag)
    except Stop as e:
        sys.exit(f"stopped: {e}\nrerun later to resume.")


if __name__ == "__main__":
    main()
