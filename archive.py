#!/usr/bin/env python3
"""
Copy every published release to the Internet Archive, one item per release
(lcsh-mirror-YYYYMMDD), so the history outlives the download server.

    python archive.py            # upload releases that aren't on archive.org yet
    python archive.py --dry-run  # only say what would be uploaded

Needs the official `internetarchive` package and a one-time `ia configure`
(archive.org account). Releases already uploaded are listed in
data/archived.txt; an upload counts only once archive.org has every file with
the same checksum as here. Exit code 1 if anything failed.
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "publish", "lcsh")
DONE = os.path.join(HERE, "data", "archived.txt")
PAGE = "https://bibliotecaalonsocossio.com/lcshmirror/"


def identifier(date):
    return f"lcsh-mirror-{date}"


def metadata(r):
    d = f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]}"
    desc = (f"Every Library of Congress Subject Heading (LCSH) as a MARC 21 authority record, "
            f"{r['count']:,} records, taken from id.loc.gov on {d}. MARC 21 and MARCXML, a CSV list "
            f"(LCCN, 005, heading)")
    if "new" in r:
        desc += (f", and the changes since the previous release ({r['new']} new, {r['changed']} changed, "
                 f"{r['deleted']} deleted)")
    desc += (f". Unofficial; not affiliated with or endorsed by the Library of Congress. "
             f"Weekly releases: {PAGE} Code: https://github.com/iratedemon04/lcsh-mirror")
    return {"title": f"LCSH mirror {d}: Library of Congress Subject Headings (MARC 21)",
            "mediatype": "data", "date": d, "creator": "Alonso Cossío Vázquez",
            "subject": ["LCSH", "Library of Congress Subject Headings", "MARC 21", "MARCXML",
                        "authority records", "cataloging", "library metadata"],
            "description": desc, "licenseurl": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": "https://id.loc.gov/authorities/subjects.html", "language": "eng"}


def archived():
    if not os.path.exists(DONE):
        return set()
    with open(DONE, encoding="utf-8") as fh:
        return {line.strip() for line in fh if line.strip()}


def verified(ia, item_id, files):
    """True when archive.org lists every file with our SHA-1."""
    item = ia.get_item(item_id)
    remote = {f.get("name"): f.get("sha1") for f in item.files}
    for f in files:
        h = hashlib.sha1()
        with open(os.path.join(OUT, f["name"]), "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if remote.get(f["name"]) != h.hexdigest():
            return False
    return True


def main(dry_run=False):
    with open(os.path.join(OUT, "releases.json"), encoding="utf-8") as fh:
        releases = json.load(fh)
    todo = [r for r in reversed(releases) if identifier(r["date"]) not in archived()]
    if not todo:
        print("archive: nothing new")
        return 0
    if dry_run:
        for r in todo:
            print("would upload", identifier(r["date"]), [f["name"] for f in r["files"]])
        return 0
    import internetarchive as ia
    failed = []
    for r in todo:
        item_id = identifier(r["date"])
        paths = [os.path.join(OUT, f["name"]) for f in r["files"]]
        try:
            ia.upload(item_id, files=paths, metadata=metadata(r), checksum=True, retries=5, retries_sleep=60)
            if not verified(ia, item_id, r["files"]):
                raise RuntimeError("files on archive.org don't match the local checksums yet")
        except Exception as e:
            failed.append(f"{item_id}: {e}")
            continue
        with open(DONE, "a", encoding="utf-8") as fh:
            fh.write(item_id + "\n")
        print(f"archived https://archive.org/details/{item_id}")
    for f in failed:
        print(f"archive FAILED {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main("--dry-run" in sys.argv))
