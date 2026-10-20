#!/usr/bin/env python3
"""
Prepare a release for download and, optionally, upload it.

    python publish.py                        # stage only, into data/publish/lcsh/
    python publish.py user@host:/path/lcsh/  # stage, then rsync there

Staging gzips the newest release in data/release/, writes latest.json (what the
download page reads), keeps fixed-name lcsh-latest.* copies for scripts and
links, and keeps the last KEEP releases. Uploading mirrors the staging folder
with rsync; latest.json goes last, so the page never lists a file that isn't
there yet.
"""
import glob
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

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
    print(f"staged {d}: {count} records in {out}")
    return d


def upload(out, dest):
    base = ["rsync", "-rtH", "--chmod=D755,F644", "--delete-after"]
    subprocess.run(base + ["--exclude", "latest.json", out.rstrip("/") + "/", dest], check=True)
    subprocess.run(["rsync", "-t", "--chmod=F644", os.path.join(out, "latest.json"), dest], check=True)
    print(f"uploaded to {dest}")


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "data", "publish", "lcsh")
    stage(os.path.join(here, "data", "release"), out)
    if len(sys.argv) > 1:
        upload(out, sys.argv[1])
