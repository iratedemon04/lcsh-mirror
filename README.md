# LCSH mirror

A full, current copy of **Library of Congress Subject Headings** as MARC authority
records, rebuilt weekly from LC's own id.loc.gov service, plus the small script that
builds it so anyone can make their own.

**Download the latest release:** https://bibliotecaalonsocossio.com/lcshmirror/

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

Scope: every current heading in LC's LCSH bulk file (`subjects.madsrdf.jsonld.gz`).
Genre/Form Terms and children's headings are separate vocabularies and not included.

## Build it yourself

Anyone can run exactly the same process that builds this mirror; that's the point
of it being open. If you just need the data, though, downloading a release from the
mirror or the [Internet Archive](https://archive.org/search?query=identifier%3Alcsh-mirror-*)
is quicker for you and kinder to id.loc.gov: the 16-day harvest is already done, and
each download saves LC's service about 470,000 requests.

```
pip install -r requirements.txt
python harvest.py               # first run ~16 days, later runs minutes; resumable
python load.py data/release/lcsh-YYYYMMDD.mrc   # optional: searchable SQLite (a downloaded .mrc.gz works too)
python tui.py libel             # optional: terminal browser (Linux/macOS)
python publish.py [user@host:/path/]   # optional: gzip + latest.json, then rsync
```

`harvest.py` reads LC's weekly bulk file to learn which headings exist and when each
last changed, then downloads each new or changed heading as MARCXML from
`id.loc.gov/authorities/subjects/<id>.marcxml.xml`, one request every 3.1 seconds
(id.loc.gov's robots.txt asks for 3). The first run fetches everything; after that,
only what LC changed. A "slow down" answer stops the run; a server hiccup pauses
5 minutes and retries a few times, then stops. Rerun and it resumes. Please keep
the pace: it's a shared public service.

Progress is written to `data/dash/` (`status.json` + a small `index.html`) for a
dashboard. The build only runs when every heading is fetched.

## Say hello

If you use this mirror, I'd love to hear from you, even if it's just a line about which
library you're at or what you're using it for. Questions, bugs, a format you'd like, or a
record that doesn't import cleanly are all welcome too.

[Open an issue](https://github.com/iratedemon04/lcsh-mirror/issues) or write to me at
iratedemon04 at gmail dot com. I read everything, and I'm happy to answer in English or Spanish.

## Licence

Code: MIT (see `LICENSE`). Data: LC authority data is a US Government work, free of
copyright in the US; this compilation adds nothing and is dedicated to the public
domain (CC0). Source: Library of Congress, via https://id.loc.gov/.
