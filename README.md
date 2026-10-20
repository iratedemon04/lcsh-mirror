# LCSH mirror

A full, current copy of **Library of Congress Subject Headings** as MARC authority
records, rebuilt weekly from LC's own public SRU server, plus the small script that
builds it so anyone can make their own.

**Unofficial.** Not affiliated with or endorsed by the Library of Congress or the US Government.

## What you get

Each release (`lcsh-YYYYMMDD.*`):

| file | contents |
|---|---|
| `lcsh-YYYYMMDD.mrc` | every LCSH authority record, MARC 21 (UTF-8) |
| `lcsh-YYYYMMDD.mrcxml` | the same records as MARCXML |
| `manifest-YYYYMMDD.csv` | one row per record: LCCN, 005 (last changed at LC), heading |
| `SHA256SUMS-YYYYMMDD` | checksums |

Records are exactly as LC serves them, including 005/008/040. Only the MARC
leader's computed length/base-address positions are recalculated when writing `.mrc`.
Open them with MarcEdit (MarcBreaker) or any MARC tool.

Scope: records whose 010 $a is an `sh` LCCN. LC Genre/Form Terms and children's
headings, which share LC's subject database, are left out.

## Build it yourself

```
pip install -r requirements.txt
python harvest.py               # a few hours; resumable, rerun to continue
python load.py data/release/lcsh-YYYYMMDD.mrc   # optional: searchable SQLite
python tui.py libel             # optional: terminal browser (Linux/macOS)
python publish.py [user@host:/path/]   # optional: gzip + latest.json, then rsync
```

`harvest.py` asks LC's SRU server (`lx2.loc.gov:210/SAF`) for 200 records at a time,
one request at a time, and waits at least 3 seconds, or as long as the server took,
before the next. Any error stops the run; nothing is hammered or retried in a loop.
Please keep it that way: it's a shared public service.

The build checks that every record SRU announced arrived and that the result set
didn't change size during the harvest. If it did, rerun.

## Licence

Code: MIT (see `LICENSE`). Data: LC authority data is a US Government work, free of
copyright in the US; this compilation adds nothing and is dedicated to the public
domain (CC0). Source: Library of Congress, via SRU `http://lx2.loc.gov:210/SAF`.
