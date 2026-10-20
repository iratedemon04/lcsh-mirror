"""Offline tests: no network. Run with  python -m unittest  from the repo root."""
import csv
import hashlib
import io
import os
import re
import sys
import tempfile
import unittest

import pymarc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import harvest  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def rec(name):
    with open(os.path.join(FIX, name + ".xml"), encoding="utf-8") as fh:
        return re.sub(r"<\?xml[^>]*\?>", "", fh.read())


SH = [rec("sh85076432"), rec("sh2007005138"), rec("sh85148273")]
GF = rec("gf2011026341")
# What bath.lccn matches on. The gf record is reached through a cancelled sh LCCN
# in its 010 $z, as happens on LC's server; build() must drop it.
RECORDS = [("sh 85076432", SH[0]), ("sh2007005138", SH[1]), ("sh 85148273", SH[2]), ("sh 85000001", GF)]


def page(records, total):
    """A searchRetrieveResponse shaped like the ones lx2.loc.gov returns."""
    body = "".join(f"<zs:record><zs:recordSchema>marcxml</zs:recordSchema><zs:recordData>{r}"
                   f"</zs:recordData></zs:record>" for r in records)
    return (f'<?xml version="1.0"?><zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">'
            f"<zs:version>1.1</zs:version><zs:numberOfRecords>{total}</zs:numberOfRecords>"
            f"<zs:records>{body}</zs:records></zs:searchRetrieveResponse>").encode()


class Server:
    """Fake SRU: bath.lccn="<prefix>*" over RECORDS. Records every call."""

    def __init__(self, records=RECORDS, fail_at=None, short=None, took=0.5):
        self.records, self.fail_at, self.short, self.took = records, fail_at, short, took
        self.calls = []

    def __call__(self, prefix, start, maximum):
        self.calls.append((prefix, start, maximum))
        if (prefix, start) == self.fail_at:
            raise harvest.Stop("connection dropped")
        match = [x for k, x in self.records if k.startswith(prefix)]
        chunk = match[start - 1:start - 1 + maximum]
        if (prefix, start) == self.short:
            chunk = chunk[:-1]
        raw = page(chunk, len(match))
        return (raw, *harvest.parse_page(raw, prefix), self.took)


class Base(unittest.TestCase):
    """Small chunks and pages so four records exercise splitting and paging."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = os.path.join(self.tmp.name, "pages")
        self.out = os.path.join(self.tmp.name, "release")
        self.saved = harvest.PAGE, harvest.CHUNK
        harvest.PAGE, harvest.CHUNK = 1, 2

    def tearDown(self):
        harvest.PAGE, harvest.CHUNK = self.saved
        self.tmp.cleanup()

    def pagefiles(self):
        return sorted(f for f in os.listdir(self.pages) if f.endswith(".xml"))


class PlanTests(Base):
    def test_splits_until_every_chunk_fits(self):
        os.makedirs(self.pages)
        self.assertEqual(harvest.plan(self.pages, Server()), ["sh 850", "sh 851", "sh2"])

    def test_counts_are_cached_across_restarts(self):
        os.makedirs(self.pages)
        harvest.plan(self.pages, Server())
        again = Server()
        self.assertEqual(harvest.plan(self.pages, again), ["sh 850", "sh 851", "sh2"])
        self.assertEqual(again.calls, [])


class FetchTests(Base):
    def test_fetches_every_page_shallow(self):
        srv = Server()
        harvest.fetch(self.pages, srv)
        self.assertEqual(self.pagefiles(), ["sh2-0000001.xml", "sh_850-0000001.xml",
                                            "sh_850-0000002.xml", "sh_851-0000001.xml"])
        self.assertTrue(all(start <= harvest.CHUNK for _, start, _ in srv.calls))

    def test_resume_skips_what_is_on_disk(self):
        with self.assertRaises(harvest.Stop):
            harvest.fetch(self.pages, Server(fail_at=("sh 850", 2)))
        self.assertEqual(self.pagefiles(), ["sh_850-0000001.xml"])
        srv = Server()
        harvest.fetch(self.pages, srv)
        self.assertEqual(srv.calls, [("sh 850", 2, 1), ("sh 851", 1, 1), ("sh2", 1, 1)])

    def test_short_page_stops_without_saving_it(self):
        with self.assertRaises(harvest.Stop):
            harvest.fetch(self.pages, Server(short=("sh 851", 1)))
        self.assertNotIn("sh_851-0000001.xml", self.pagefiles())

    def test_sru_diagnostic_stops(self):
        raw = (b'<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/"><zs:diagnostics>'
               b'<d:diagnostic xmlns:d="http://www.loc.gov/zing/srw/diagnostic/"><d:message>System '
               b'temporarily unavailable</d:message></d:diagnostic></zs:diagnostics></zs:searchRetrieveResponse>')
        with self.assertRaisesRegex(harvest.Stop, "temporarily unavailable"):
            harvest.parse_page(raw, "x")


class PacingTests(unittest.TestCase):
    def ask(self, took):
        slept = []
        g = harvest.Gentle(call=Server(took=took), sleep=slept.append)
        g("sh2", 1, 1)
        return slept

    def test_waits_ten_times_the_server_time(self):
        self.assertEqual(self.ask(6.0), [60.0])

    def test_never_waits_less_than_the_minimum(self):
        self.assertEqual(self.ask(0.2), [harvest.MIN_WAIT])

    def test_slow_answer_stops_the_run(self):
        with self.assertRaisesRegex(harvest.Stop, "backing off"):
            self.ask(harvest.SLOW + 1)


class BuildTests(Base):
    def test_release_keeps_lcsh_only_once_each(self):
        harvest.fetch(self.pages, Server(RECORDS + [("sh 85176432", SH[0])]))   # + a duplicate
        self.assertTrue(harvest.build(self.pages, self.out, date="20261020"))
        with open(os.path.join(self.out, "lcsh-20261020.mrc"), "rb") as fh:
            got = sorted(harvest.lccn(r) for r in pymarc.MARCReader(fh, to_unicode=True, force_utf8=True))
        self.assertEqual(got, ["sh 85076432", "sh 85148273", "sh2007005138"])
        with open(os.path.join(self.out, "lcsh-20261020.mrcxml"), "rb") as fh:
            self.assertEqual(len(pymarc.parse_xml_to_array(fh)), 3)
        with open(os.path.join(self.out, "manifest-20261020.csv"), encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["lccn", "005", "heading"])
        self.assertEqual([r[0] for r in rows[1:]], ["sh 85076432", "sh 85148273", "sh2007005138"])
        self.assertTrue(all(r[1] and r[2] for r in rows[1:]))

    def test_checksums_match_files(self):
        harvest.fetch(self.pages, Server())
        harvest.build(self.pages, self.out, date="20261020")
        with open(os.path.join(self.out, "SHA256SUMS-20261020"), encoding="utf-8") as fh:
            lines = fh.read().split("\n")[:-1]
        self.assertEqual(len(lines), 3)
        for line in lines:
            digest, name = line.split("  ")
            with open(os.path.join(self.out, name), "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest)

    def test_round_trip_is_faithful(self):
        """The .mrc holds exactly what LC sent: same fields, order, values. Only the
        leader's computed positions (record length 00-04, base address 12-16) may differ."""
        harvest.fetch(self.pages, Server())
        harvest.build(self.pages, self.out, date="20261020")
        src = {harvest.lccn(r): r for r in (pymarc.parse_xml_to_array(io.StringIO(x))[0] for x in SH)}
        with open(os.path.join(self.out, "lcsh-20261020.mrc"), "rb") as fh:
            out = list(pymarc.MARCReader(fh, to_unicode=True, force_utf8=True))
        self.assertEqual(len(out), 3)
        for b in out:
            a = src[harvest.lccn(b)]
            la, lb = str(a.leader), str(b.leader)
            self.assertEqual(la[5:12] + la[17:], lb[5:12] + lb[17:])
            self.assertEqual([str(f) for f in a.get_fields()], [str(f) for f in b.get_fields()])

    def test_chunk_changing_mid_harvest_is_flagged(self):
        harvest.fetch(self.pages, Server())
        # pretend LC added a record to "sh 850" between its first and last page
        with open(os.path.join(self.pages, "sh_850-0000002.xml"), "wb") as fh:
            fh.write(page([GF], 3))
        self.assertFalse(harvest.build(self.pages, self.out, date="20261020"))

    def test_missing_page_is_flagged(self):
        harvest.fetch(self.pages, Server())
        os.remove(os.path.join(self.pages, "sh_850-0000002.xml"))
        self.assertFalse(harvest.build(self.pages, self.out, date="20261020"))


if __name__ == "__main__":
    unittest.main()
