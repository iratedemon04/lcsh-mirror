#!/usr/bin/env python3
"""
Load a release .mrc into a searchable SQLite database (full-text search on
headings and see-references).

    python load.py data/release/lcsh-20261020.mrc      # -> data/lcsh.db

The database is rebuilt from scratch each time and swapped in atomically, so a
search running during a reload keeps working.
"""
import os
import sqlite3
import sys

import pymarc

SCHEMA = """
CREATE TABLE records(lccn TEXT PRIMARY KEY, heading TEXT, marc BLOB);
CREATE VIRTUAL TABLE fts USING fts5(lccn UNINDEXED, heading, refs,
                                    tokenize='unicode61 remove_diacritics 2');
"""


def heading(rec):
    f = next((f for f in rec.get_fields() if f.tag.startswith("1")), None)
    return "--".join(sf.value.strip() for sf in f.subfields if sf.code in "avxyz") if f else ""


def refs(rec):
    return " | ".join(" ".join(sf.value for sf in f.subfields if sf.code in "avxyz")
                      for f in rec.get_fields() if f.tag[0] in "45" and not f.is_control_field())


def connect(db):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def preview(marc):
    return str(next(pymarc.MARCReader(marc, to_unicode=True, force_utf8=True)))


def load(mrc, db):
    tmp = db + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.executescript(SCHEMA)
    n = 0
    with open(mrc, "rb") as fh:
        for rec in pymarc.MARCReader(fh, to_unicode=True, force_utf8=True):
            lccn = rec["010"]["a"].strip()
            h = heading(rec)
            c.execute("INSERT INTO records VALUES(?,?,?)", (lccn, h, rec.as_marc()))
            c.execute("INSERT INTO fts VALUES(?,?,?)", (lccn, h, refs(rec)))
            n += 1
    c.commit()
    c.close()
    os.replace(tmp, db)
    return n


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    mrc = sys.argv[1]
    db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(mrc))), "lcsh.db")
    print(f"{load(mrc, db)} records loaded into {db}")
