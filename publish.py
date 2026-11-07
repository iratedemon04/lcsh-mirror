#!/usr/bin/env python3
"""
Prepare a release for download and, optionally, upload it.

    python publish.py                        # stage only, into data/publish/lcsh/
    python publish.py user@host:/path/lcsh/  # stage, then rsync there

Staging gzips the newest release in data/release/, writes latest.json and the
static HTML pieces of the download page, keeps fixed-name lcsh-latest.* copies for
scripts and links, and keeps the last KEEP_PUBLISHED releases (KEEP locally). Uploading mirrors the folder
with rsync; pages, feed and JSON go last, so nothing ever lists a file that isn't
there yet.
"""
import csv
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

KEEP = 4              # uncompressed releases kept locally (the previous one is needed for change files)
KEEP_PUBLISHED = 50   # releases kept on the download server
SHOW_PREVIOUS = 3     # previous releases listed on the page (all of them are in the file listing)
FEED = 20             # releases in the Atom feed
# (source in data/release, fixed-name alias, label en, label es). The last four only exist
# when there was an earlier release to compare with.
FILES = [("lcsh-{d}.mrc", "lcsh-latest.mrc.gz", "MARC 21", "MARC 21"),
         ("lcsh-{d}.mrcxml", "lcsh-latest.mrcxml.gz", "MARCXML", "MARCXML"),
         ("manifest-{d}.csv", "manifest-latest.csv.gz", "CSV", "CSV"),
         ("changes-{d}.mrc", "changes-latest.mrc.gz", "new or changed, MARC 21", "nuevos o modificados, MARC 21"),
         ("changes-{d}.mrcxml", "changes-latest.mrcxml.gz", "new or changed, MARCXML", "nuevos o modificados, MARCXML"),
         ("changes-{d}.csv", "changes-latest.csv.gz", "new or changed, CSV", "nuevos o modificados, CSV"),
         ("deleted-{d}.csv", "deleted-latest.csv.gz", "deleted, CSV", "eliminados, CSV")]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_rows(gz):
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        return sum(1 for _ in fh) - 1


def newest_release(release):
    dates = sorted(m.group(1) for f in os.listdir(release) if (m := re.fullmatch(r"lcsh-(\d{8})\.mrc", f)))
    if not dates:
        sys.exit(f"no release in {release}; run harvest.py first")
    return dates[-1]


def stage(release, out):
    d = newest_release(release)
    os.makedirs(out, exist_ok=True)
    files = []
    for src, latest, *_ in FILES:
        src = os.path.join(release, src.format(d=d))
        gz = os.path.join(out, os.path.basename(src) + ".gz")
        if not os.path.exists(src):
            if os.path.exists(os.path.join(out, latest)):
                os.remove(os.path.join(out, latest))
            continue
        if not os.path.exists(gz):
            with open(src, "rb") as fi, gzip.open(gz + ".part", "wb", compresslevel=9) as fo:
                shutil.copyfileobj(fi, fo, 1 << 20)
            os.replace(gz + ".part", gz)
        link = os.path.join(out, latest)
        if os.path.exists(link):
            os.remove(link)
        try:
            os.link(gz, link)            # same bytes, no extra disk
        except OSError:
            shutil.copyfile(gz, link)
        files.append({"name": os.path.basename(gz), "size": os.path.getsize(gz), "sha256": sha256(gz)})
    # (labels and change counts are added from releases.json below)

    with open(os.path.join(release, f"manifest-{d}.csv"), encoding="utf-8") as fh:
        count = sum(1 for _ in fh) - 1
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as fh:
        for f in files:
            fh.write(f"{f['sha256']}  {f['name']}\n")

    # keep the newest KEEP_PUBLISHED releases online and KEEP uncompressed ones locally
    for folder, keep in ((out, KEEP_PUBLISHED), (release, KEEP)):
        old = sorted({m.group(1) for f in os.listdir(folder)
                      if (m := re.match(r"(?:lcsh|manifest|changes|deleted|SHA256SUMS)-(\d{8})", f))})[:-keep]
        for o in old:
            for f in glob.glob(os.path.join(folder, f"*-{o}*")):
                os.remove(f)

    # every release still staged, newest first: the page lists the older ones
    releases = []
    for rd in sorted({m.group(1) for f in os.listdir(out)
                      if (m := re.fullmatch(r"lcsh-(\d{8})\.mrc\.gz", f))}, reverse=True):
        found = [(os.path.join(out, f[0].format(d=rd) + ".gz"), f) for f in FILES]
        found = [(p, f) for p, f in found if os.path.exists(p)]
        if len(found) < 3:
            continue
        r = {"date": rd, "count": csv_rows(found[2][0]), "files": [
            {"name": os.path.basename(p), "size": os.path.getsize(p), "sha256": sha256(p),
             "label": f[2], "label_es": f[3]} for p, f in found]}
        changes = os.path.join(out, f"changes-{rd}.csv.gz")
        if os.path.exists(changes):
            with gzip.open(changes, "rt", encoding="utf-8") as fh:
                status = [row[1] for row in list(csv.reader(fh))[1:]]
            r.update(new=status.count("new"), changed=status.count("changed"),
                     deleted=csv_rows(os.path.join(out, f"deleted-{rd}.csv.gz")))
        releases.append(r)
    with open(os.path.join(out, "releases.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(releases, fh, indent=1)
    with open(os.path.join(out, "latest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(releases[0], fh, indent=1)
    render(out, releases)
    print(f"staged {d}: {count} records in {out}")
    return d


# ---------------------------------------------------------------- static pages
# Plain HTML, no scripts: the download page pulls the snippets in with Server Side
# Includes, and lcsh/ gets an "Index of" listing, a README.txt and an Atom feed.

SITE = "https://bibliotecaalonsocossio.com"
URL = "/lcshmirror/lcsh/"
ARCHIVE = "https://archive.org/search?query=identifier%3Alcsh-mirror-%2A"
WORD = {"en": "records", "es": "registros"}
DELTA = {"en": "Since {prev}: {new:,} new, {changed:,} changed, {deleted:,} deleted.",
         "es": "Desde {prev}: {new:,} nuevos, {changed:,} modificados, {deleted:,} eliminados."}
POINTERS = ["current.en.html", "current.es.html", "previous.en.html", "previous.es.html", "stats.en.html", "stats.es.html",
            "index.html", "README.txt", "releases.atom", "releases.json", "latest.json", "SHA256SUMS"]


def iso(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def human(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB" if n >= 1e3 else f"{n} bytes"


def rows(r, lang):
    key = "label" if lang == "en" else "label_es"
    return "".join(f'<tr><td><a href="{URL}{f["name"]}">{f["name"]}</a></td><td class="sz">{human(f["size"])}</td>'
                   f'<td>{f[key]}</td></tr>\n' for f in r["files"])


def delta(r, prev, lang):
    return DELTA[lang].format(prev=iso(prev["date"]), **r) if prev and "new" in r else ""


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


STATS = {
    "en": "{count} headings. Since {since}: {net} net ({netd}/day), {changed} new or changed ({chd}/day).",
    "es": "{count} encabezamientos. Desde {since}: {net} netos ({netd}/día), {changed} nuevos o modificados ({chd}/día).",
    "en0": "{count} headings.",
    "es0": "{count} encabezamientos.",
}


def stats(lang, count, history):
    """One line: the count, plus averages between LC's weekly lists (our own measurements)."""
    if len(history) < 2:
        return "<p>" + STATS[lang + "0"].format(count=f"{count:,}") + "</p>\n"
    first, last = history[0], history[-1]
    days = max(1, (dt.date.fromisoformat(last[0]) - dt.date.fromisoformat(first[0])).days)
    net = sum(int(r[2]) for r in history[1:])
    changed = sum(int(r[3]) for r in history[1:])
    return "<p>" + STATS[lang].format(count=f"{count:,}", since=first[0], net=f"{net:+,}", netd=f"{net / days:.1f}",
                                      changed=f"{changed:,}", chd=f"{changed / days:.1f}") + "</p>\n"


def render(out, releases):
    cur, prev = releases[0], releases[1:]
    hist_path = os.path.join(os.path.dirname(os.path.dirname(out)), "history.csv")
    history = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as fh:
            history = list(csv.reader(fh))[1:]
    for lang in ("en", "es"):
        write(os.path.join(out, f"stats.{lang}.html"), stats(lang, cur["count"], history))
        n = f'{cur["count"]:,} {WORD[lang]}'
        d = delta(cur, prev[0] if prev else None, lang)
        write(os.path.join(out, f"current.{lang}.html"),
              f'<p>{iso(cur["date"])}, {n}.{" " + d if d else ""}</p>\n<table class="files">\n{rows(cur, lang)}</table>\n'
              f'<p class="small"><a href="{URL}SHA256SUMS">SHA256SUMS</a> &middot; '
              f'<a href="{URL}">{"all files" if lang == "en" else "todos los archivos"}</a> &middot; '
              f'<a href="{URL}releases.atom">Atom</a> &middot; '
              f'<a href="{ARCHIVE}">{"all releases on the Internet Archive" if lang == "en" else "todas las versiones en el Internet Archive"}</a></p>\n')
        write(os.path.join(out, f"previous.{lang}.html"),
              "".join(f'<h3>{iso(r["date"])}, {r["count"]:,} {WORD[lang]}</h3>\n'
                      + (f'<p class="small">{delta(r, prev[i + 1], lang)}</p>\n' if i + 1 < len(prev) and "new" in r else "")
                      + f'<table class="files">\n{rows(r, lang)}</table>\n'
                      for i, r in enumerate(prev[:SHOW_PREVIOUS]))
              + (f'<p class="small"><a href="{URL}">{"Older releases" if lang == "en" else "Versiones más antiguas"}</a></p>\n'
                 if len(prev) > SHOW_PREVIOUS else "") or "<p>&ndash;</p>\n")

    # README.txt
    lines = [f"LCSH mirror: {SITE}/lcshmirror/", "",
             "Every Library of Congress Subject Heading as a MARC authority record,",
             "taken from id.loc.gov and rebuilt weekly. Unofficial; not affiliated with",
             "or endorsed by the Library of Congress. Data: public domain (CC0).", "",
             f"Current release: {iso(cur['date'])}, {cur['count']:,} records"]
    if prev and "new" in cur:
        lines += [delta(cur, prev[0], "en")]
    lines += [""] + [f"  {f['name']:<30} {human(f['size']):>7}  {f['label']}" for f in cur["files"]]
    lines += ["", "*-latest.* always point to the current release. changes-* hold only the records",
              "new or changed since the previous release; deleted-* lists headings removed since then.", "",
              "Verify:   sha256sum -c SHA256SUMS", "Read:     MarcEdit > MarcBreaker (.mrc -> .mrk)",
              "Feed:     releases.atom", "Archive:  " + ARCHIVE, "Code:     https://github.com/iratedemon04/lcsh-mirror", ""]
    write(os.path.join(out, "README.txt"), "\n".join(lines))

    # Atom feed, one entry per release still on the server
    now = f"{iso(cur['date'])}T00:00:00Z"
    entries = "".join(
        f"  <entry>\n    <title>LCSH release {iso(r['date'])}: {r['count']:,} records"
        f"{(' (' + str(r['new']) + ' new, ' + str(r['changed']) + ' changed, ' + str(r['deleted']) + ' deleted)') if 'new' in r else ''}</title>\n"
        f"    <id>tag:bibliotecaalonsocossio.com,2026:lcsh-{r['date']}</id>\n"
        f"    <updated>{iso(r['date'])}T00:00:00Z</updated>\n"
        f'    <link href="{SITE}/lcshmirror/"/>\n'
        f"    <content type=\"text\">{' '.join(SITE + URL + f['name'] for f in r['files'])}</content>\n  </entry>\n"
        for r in releases[:FEED])
    write(os.path.join(out, "releases.atom"),
          '<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n'
          f"  <title>LCSH mirror releases</title>\n  <id>{SITE}/lcshmirror/</id>\n  <updated>{now}</updated>\n"
          f'  <link href="{SITE}/lcshmirror/"/>\n  <link rel="self" href="{SITE}{URL}releases.atom"/>\n'
          f"  <author><name>Alonso Cossío Vázquez</name></author>\n{entries}</feed>\n")

    # "Index of" listing, like a classic server directory listing
    names = sorted(f for f in os.listdir(out) if not f.startswith(("current.", "previous.", "stats.", "index.")))
    width = max(len(f) for f in names) + 2
    listing = "".join(
        f'<a href="{f}">{f}</a>{" " * (width - len(f))}'
        f'{time.strftime("%Y-%m-%d %H:%M", time.gmtime(os.path.getmtime(os.path.join(out, f))))}'
        f'{os.path.getsize(os.path.join(out, f)):>14}\n' for f in names)
    write(os.path.join(out, "index.html"),
          f"<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\"><title>Index of {URL}</title></head>\n"
          f"<body>\n<h1>Index of {URL}</h1><hr><pre><a href=\"../\">../</a>\n{listing}</pre><hr>\n"
          f'<p>Read <a href="README.txt">README.txt</a> first. '
          f'<a href="/lcshmirror/">LCSH mirror</a></p>\n</body></html>\n')


def upload(out, dest):
    """Data files first, then everything that points at them, so nothing links a missing file."""
    base = ["rsync", "-rtH", "--chmod=D755,F644"]
    skip = [x for p in POINTERS for x in ("--exclude", p)]
    subprocess.run(base + skip + [out.rstrip("/") + "/", dest], check=True)
    subprocess.run(base + ["--delete-after", out.rstrip("/") + "/", dest], check=True)
    print(f"uploaded to {dest}")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "data", "publish", "lcsh")
    stage(os.path.join(here, "data", "release"), out)
    if len(sys.argv) > 1:
        upload(out, sys.argv[1])
