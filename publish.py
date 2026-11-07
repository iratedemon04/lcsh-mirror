#!/usr/bin/env python3
"""
Prepare a release for download and, optionally, upload it.

    python publish.py                        # stage only, into data/publish/lcsh/
    python publish.py user@host:/path/lcsh/  # stage, then rsync there

Staging gzips the newest release in data/release/, writes latest.json and the
static HTML pieces of the download page, keeps fixed-name lcsh-latest.* copies for
scripts and links, and keeps the last KEEP releases. Uploading mirrors the folder
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

KEEP = 4
FILES = [("lcsh-{d}.mrc", "lcsh-latest.mrc.gz"),
         ("lcsh-{d}.mrcxml", "lcsh-latest.mrcxml.gz"),
         ("manifest-{d}.csv", "manifest-latest.csv.gz")]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def newest_release(release):
    dates = sorted(m.group(1) for f in os.listdir(release) if (m := re.fullmatch(r"lcsh-(\d{8})\.mrc", f)))
    if not dates:
        sys.exit(f"no release in {release}; run harvest.py first")
    return dates[-1]


def stage(release, out):
    d = newest_release(release)
    os.makedirs(out, exist_ok=True)
    files = []
    for src, latest in FILES:
        src = os.path.join(release, src.format(d=d))
        gz = os.path.join(out, os.path.basename(src) + ".gz")
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

    with open(os.path.join(release, f"manifest-{d}.csv"), encoding="utf-8") as fh:
        count = sum(1 for _ in fh) - 1
    with open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as fh:
        for f in files:
            fh.write(f"{f['sha256']}  {f['name']}\n")
    with open(os.path.join(out, "latest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"date": d, "count": count, "files": files}, fh, indent=1)

    # keep the newest KEEP releases, here and in the uncompressed release folder
    for folder in (out, release):
        old = sorted({m.group(1) for f in os.listdir(folder)
                      if (m := re.match(r"(?:lcsh|manifest|SHA256SUMS)-(\d{8})", f))})[:-KEEP]
        for o in old:
            for f in glob.glob(os.path.join(folder, f"*-{o}*")):
                os.remove(f)

    # every release still staged, newest first: the page lists the older ones
    releases = []
    for rd in sorted({m.group(1) for f in os.listdir(out)
                      if (m := re.fullmatch(r"lcsh-(\d{8})\.mrc\.gz", f))}, reverse=True):
        paths = [os.path.join(out, src.format(d=rd) + ".gz") for src, _ in FILES]
        if not all(os.path.exists(p) for p in paths):
            continue
        with gzip.open(paths[2], "rt", encoding="utf-8") as fh:
            rcount = sum(1 for _ in fh) - 1
        releases.append({"date": rd, "count": rcount, "files": [
            {"name": os.path.basename(p), "size": os.path.getsize(p), "sha256": sha256(p)} for p in paths]})
    with open(os.path.join(out, "releases.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(releases, fh, indent=1)
    render(out, releases)
    print(f"staged {d}: {count} records in {out}")
    return d


# ---------------------------------------------------------------- static pages
# Plain HTML, no scripts: the download page pulls the snippets in with Server Side
# Includes, and lcsh/ gets an "Index of" listing, a README.txt and an Atom feed.

SITE = "https://bibliotecaalonsocossio.com"
URL = "/lcshmirror/lcsh/"
LABEL = ["MARC 21", "MARCXML", "CSV"]
WORD = {"en": "records", "es": "registros"}
POINTERS = ["current.en.html", "current.es.html", "previous.en.html", "previous.es.html", "stats.en.html", "stats.es.html",
            "index.html", "README.txt", "releases.atom", "releases.json", "latest.json", "SHA256SUMS"]


def iso(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def human(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def rows(r):
    return "".join(f'<tr><td><a href="{URL}{f["name"]}">{f["name"]}</a></td><td class="sz">{human(f["size"])}</td>'
                   f'<td>{LABEL[i]}</td></tr>\n' for i, f in enumerate(r["files"]))


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


STATS = {
    "en": dict(headings="Headings", growth="Growth", changed="New or changed", span="Measured",
               days="days", per="per day", upd=("update", "updates"), sofar="so far",
               first="Averages start with the next weekly update."),
    "es": dict(headings="Encabezamientos", growth="Crecimiento", changed="Nuevos o modificados", span="Medido",
               days="días", per="por día", upd=("actualización", "actualizaciones"), sofar="hasta ahora",
               first="Los promedios empiezan con la próxima actualización semanal."),
}


def stats(lang, count, history):
    """The counter: our own measurements between LC's weekly bulk lists, averaged over all of them."""
    s = STATS[lang]
    out = f'<table class="stats">\n<tr><td>{s["headings"]}</td><td><b>{count:,}</b></td></tr>\n'
    if len(history) < 2:
        return out + f'</table>\n<p class="small">{s["first"]}</p>\n'
    first, last = history[0], history[-1]
    days = max(1, (dt.date.fromisoformat(last[0]) - dt.date.fromisoformat(first[0])).days)
    net = sum(int(r[2]) for r in history[1:])
    changed = sum(int(r[3]) for r in history[1:])
    n = len(history) - 1
    return out + (
        f'<tr><td>{s["growth"]}</td><td>{net:+,} / {days} {s["days"]} &asymp; {net / days:+.1f} {s["per"]}</td></tr>\n'
        f'<tr><td>{s["changed"]}</td><td>{changed:,} / {days} {s["days"]} &asymp; {changed / days:.1f} {s["per"]}</td></tr>\n'
        f'<tr><td>{s["span"]}</td><td>{first[0]} &ndash; {last[0]} ({n} {s["upd"][n != 1]} {s["sofar"]})</td></tr>\n'
        "</table>\n")


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
        write(os.path.join(out, f"current.{lang}.html"),
              f'<p>{iso(cur["date"])}, {n}.</p>\n<table class="files">\n{rows(cur)}</table>\n'
              f'<p class="small"><a href="{URL}SHA256SUMS">SHA256SUMS</a> &middot; '
              f'<a href="{URL}">{"all files" if lang == "en" else "todos los archivos"}</a> &middot; '
              f'<a href="{URL}releases.atom">Atom</a></p>\n')
        write(os.path.join(out, f"previous.{lang}.html"),
              "".join(f'<h3>{iso(r["date"])}, {r["count"]:,} {WORD[lang]}</h3>\n<table class="files">\n{rows(r)}</table>\n'
                      for r in prev) or "<p>&ndash;</p>\n")

    # README.txt
    lines = [f"LCSH mirror: {SITE}/lcshmirror/", "",
             "Every Library of Congress Subject Heading as a MARC authority record,",
             "taken from id.loc.gov and rebuilt weekly. Unofficial; not affiliated with",
             "or endorsed by the Library of Congress. Data: public domain (CC0).", "",
             f"Current release: {iso(cur['date'])}, {cur['count']:,} records", ""]
    lines += [f"  {f['name']:<28} {human(f['size']):>7}  {LABEL[i]}" for i, f in enumerate(cur["files"])]
    lines += ["", "lcsh-latest.* and manifest-latest.* always point to the current release.", "",
              "Verify:   sha256sum -c SHA256SUMS", "Read:     MarcEdit > MarcBreaker (.mrc -> .mrk)",
              "Feed:     releases.atom", "Code:     https://github.com/iratedemon04/lcsh-mirror", ""]
    write(os.path.join(out, "README.txt"), "\n".join(lines))

    # Atom feed, one entry per release still on the server
    now = f"{iso(cur['date'])}T00:00:00Z"
    entries = "".join(
        f"  <entry>\n    <title>LCSH release {iso(r['date'])}: {r['count']:,} records</title>\n"
        f"    <id>tag:bibliotecaalonsocossio.com,2026:lcsh-{r['date']}</id>\n"
        f"    <updated>{iso(r['date'])}T00:00:00Z</updated>\n"
        f'    <link href="{SITE}/lcshmirror/"/>\n'
        f"    <content type=\"text\">{' '.join(SITE + URL + f['name'] for f in r['files'])}</content>\n  </entry>\n"
        for r in releases)
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
