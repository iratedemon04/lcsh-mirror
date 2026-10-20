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


def page(records, total):
    """A searchRetrieveResponse shaped like the ones lx2.loc.gov returns."""
    body = "".join(f"<zs:record><zs:recordSchema>marcxml</zs:recordSchema><zs:recordData>{r}"
                   f"</zs:recordData></zs:record>" for r in records)
    return (f'<?xml version="1.0"?><zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/">'
            f"<zs:version>1.1</zs:version><zs:numberOfRecords>{total}</zs:numberOfRecords>"
            f"<zs:records>{body}</zs:records></zs:searchRetrieveResponse>").encode()


class Server:
    """Fake SRU: serves `records` in pages of harvest.PAGE; counts calls."""

    def __init__(self, records, total=None, short=None, fail_at=None):
        self.records, self.total = records, total or len(records)
        self.short, self.fail_at, self.calls = short, fail_at, []

    def __call__(self, start):
        self.calls.append(start)
        if start == self.fail_at:
            raise harvest.Stop("connection dropped")
        chunk = self.records[start - 1:start - 1 + harvest.PAGE]
        if start == self.short:
            chunk = chunk[:-1]
        raw = page(chunk, self.total)
        return (raw, *harvest.parse_page(raw, start))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pages = os.path.join(self.tmp.name, "pages")
        self.out = os.path.join(self.tmp.name, "release")
        self.old_page, harvest.PAGE = harvest.PAGE, 2
        self.slept = []

    def tearDown(self):
        harvest.PAGE = self.old_page
        self.tmp.cleanup()

    def fetch(self, server, delay=3.0):
        return harvest.fetch(self.pages, delay, get=server, sleep=self.slept.append)


class FetchTests(Base):
    def test_fetches_every_page_and_waits_between_requests(self):
        srv = Server(SH + [GF, SH[0]])            # 5 records -> pages at 1, 3, 5
        self.assertEqual(self.fetch(srv), 5)
        self.assertEqual(srv.calls, [1, 3, 5])
        self.assertEqual(sorted(os.listdir(self.pages)), ["0000001.xml", "0000003.xml", "0000005.xml"])
        self.assertTrue(all(s >= 3.0 for s in self.slept))
        self.assertEqual(len(self.slept), 3)

    def test_resume_skips_pages_already_on_disk(self):
        with self.assertRaises(harvest.Stop):
            self.fetch(Server(SH + [GF], fail_at=3))
        self.assertEqual(os.listdir(self.pages), ["0000001.xml"])
        srv = Server(SH + [GF])
        self.fetch(srv)
        self.assertEqual(srv.calls, [3])

    def test_short_page_stops_without_saving_it(self):
        with self.assertRaises(harvest.Stop):
            self.fetch(Server(SH + [GF], short=3))
        self.assertEqual(os.listdir(self.pages), ["0000001.xml"])

    def test_sru_diagnostic_stops(self):
        raw = (b'<zs:searchRetrieveResponse xmlns:zs="http://www.loc.gov/zing/srw/"><zs:diagnostics>'
               b'<d:diagnostic xmlns:d="http://www.loc.gov/zing/srw/diagnostic/"><d:message>System '
               b'temporarily unavailable</d:message></d:diagnostic></zs:diagnostics></zs:searchRetrieveResponse>')
        with self.assertRaisesRegex(harvest.Stop, "temporarily unavailable"):
            harvest.parse_page(raw, 1)


class BuildTests(Base):
    def test_release_keeps_lcsh_only_once_each(self):
        self.fetch(Server(SH + [GF, SH[0]]))     # gf record + a duplicate
        self.assertTrue(harvest.build(self.pages, self.out, date="20261020"))
        with open(os.path.join(self.out, "lcsh-20261020.mrc"), "rb") as fh:
            recs = list(pymarc.MARCReader(fh, to_unicode=True, force_utf8=True))
        self.assertEqual([harvest.lccn(r) for r in recs], ["sh 85076432", "sh2007005138", "sh 85148273"])
        with open(os.path.join(self.out, "lcsh-20261020.mrcxml"), "rb") as fh:
            self.assertEqual(len(pymarc.parse_xml_to_array(fh)), 3)
        with open(os.path.join(self.out, "manifest-20261020.csv"), encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["lccn", "005", "heading"])
        self.assertEqual([r[0] for r in rows[1:]], ["sh 85076432", "sh 85148273", "sh2007005138"])
        self.assertTrue(all(r[1] and r[2] for r in rows[1:]))

    def test_checksums_match_files(self):
        self.fetch(Server(SH))
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
        self.fetch(Server(SH))
        harvest.build(self.pages, self.out, date="20261020")
        src = [pymarc.parse_xml_to_array(io.StringIO(r))[0] for r in SH]
        with open(os.path.join(self.out, "lcsh-20261020.mrc"), "rb") as fh:
            out = list(pymarc.MARCReader(fh, to_unicode=True, force_utf8=True))
        self.assertEqual(len(out), len(src))
        for a, b in zip(src, out):
            la, lb = str(a.leader), str(b.leader)
            self.assertEqual(la[5:12] + la[17:], lb[5:12] + lb[17:])
            self.assertEqual([str(f) for f in a.get_fields()], [str(f) for f in b.get_fields()])

    def test_result_set_changing_mid_harvest_is_flagged(self):
        harvest.fetch(self.pages, 0, get=Server(SH + [GF]), sleep=lambda s: None)
        # pretend the last page was fetched after LC added a record
        last = os.path.join(self.pages, "0000003.xml")
        with open(last, "wb") as fh:
            fh.write(page([SH[2], GF], 5))
        self.assertFalse(harvest.build(self.pages, self.out, date="20261020"))


if __name__ == "__main__":
    unittest.main()
