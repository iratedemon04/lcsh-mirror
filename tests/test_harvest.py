"""Offline tests: no network. Run with  python -m unittest  from the repo root."""
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import pymarc

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import harvest  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def body(name):
    with open(os.path.join(FIX, name + ".xml"), "rb") as fh:
        return fh.read()


IDS = ["sh2007005138", "sh85076432", "sh85148273"]
RECORDS = {i: body(i) for i in IDS}


def write_dump(path, ids_dates, deprecated=()):
    """A tiny subjects.madsrdf.jsonld.gz in LC's shape: one JSON graph per line."""
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for i, date in ids_dates:
            fh.write(json.dumps({"@graph": [
                {"@id": f"http://id.loc.gov/authorities/subjects/{i}", "@type": ["madsrdf:Authority", "madsrdf:Topic"]},
                {"@id": "_:b1", "@type": "ri:RecordInfo", "ri:recordChangeDate": {"@value": "1990-01-01T00:00:00"}},
                {"@id": "_:b2", "@type": "ri:RecordInfo", "ri:recordChangeDate": {"@value": date}},
            ]}) + "\n")
        for i in deprecated:
            fh.write(json.dumps({"@graph": [
                {"@id": f"http://id.loc.gov/authorities/subjects/{i}", "@type": ["madsrdf:DeprecatedAuthority"]}]}) + "\n")


class Site:
    """Fake id.loc.gov: serves RECORDS, or a scripted status for some ids."""

    def __init__(self, script=None):
        self.script, self.calls = dict(script or {}), []

    def __call__(self, id_):
        self.calls.append(id_)
        s = self.script.get(id_)
        if isinstance(s, list):
            s = s.pop(0) if s else None
        if isinstance(s, tuple):
            return s
        if s is not None:
            return s, b""
        return (200, RECORDS[id_]) if id_ in RECORDS else (404, b"")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = self.tmp.name
        self.c = harvest.connect(self.data)
        self.slept = []
        self.dump(1000)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def dump(self, padding, dates=None, deprecated=()):
        """Census with our three real ids plus `padding` ids that 404 (the census refuses tiny files)."""
        dates = dates or {}
        rows = [(i, dates.get(i, "2025-08-04T00:00:00")) for i in IDS]
        rows += [(f"sh9{n:07d}", "2020-01-01T00:00:00") for n in range(padding)]
        path = os.path.join(self.data, "subjects.madsrdf.jsonld.gz")
        write_dump(path, rows, deprecated)
        return harvest.census(self.c, self.data, fetch_dump=lambda url, p: self.fail("should not download"))

    def fetch(self, site=None):
        return harvest.fetch(self.c, self.data, getter=site or Site(), sleep=self.slept.append)

    def stored(self):
        return sorted(r[0] for r in self.c.execute("SELECT id FROM records"))


class CensusTests(Base):
    def test_lists_current_headings_with_their_latest_change(self):
        self.dump(1000, dates={"sh85076432": "2025-08-04T13:32:12"}, deprecated=["sh85000001"])
        changed = dict(self.c.execute("SELECT id, changed FROM census"))
        self.assertEqual(changed["sh85076432"], "2025-08-04T13:32:12")
        self.assertNotIn("sh85000001", changed)
        self.assertEqual(len(changed), 1003)

    def test_history_records_each_lc_list_once(self):
        stamp = os.path.join(self.data, "subjects.madsrdf.jsonld.gz.date")
        with open(stamp, "w") as fh:
            fh.write("2026-10-14")
        self.dump(1000)
        self.fetch()
        self.dump(1000)                                    # same list again: no new line
        with open(stamp, "w") as fh:
            fh.write("2026-11-04")
        self.dump(1010, dates={"sh85148273": "2026-10-20T00:00:00"})
        with open(os.path.join(self.data, "history.csv"), encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["list_date", "headings", "net_change", "new_or_changed"])
        self.assertEqual(rows[1:], [["2026-10-14", "1003", "", "1003"],
                                    ["2026-11-04", "1013", "10", "11"]])

    def test_refuses_a_suspiciously_small_bulk_file(self):
        with self.assertRaises(harvest.Stop):
            self.dump(0)


class FetchTests(Base):
    def test_fetches_everything_once_and_records_what_is_missing(self):
        self.assertEqual(self.fetch(), 1003)
        self.assertEqual(self.stored(), IDS)
        self.assertEqual(self.c.execute("SELECT count(*) FROM gone").fetchone()[0], 1000)
        self.assertEqual(harvest.todo(self.c), [])

    def test_keeps_lcs_pace(self):
        site = Site()
        self.fetch(site)
        self.assertTrue(all(0 < s <= harvest.DELAY for s in self.slept))
        self.assertGreaterEqual(len(self.slept), len(site.calls) - 1)

    def test_slow_down_answer_stops_and_resumes_later(self):
        with self.assertRaisesRegex(harvest.Stop, "slow down"):
            self.fetch(Site({"sh85076432": 429}))
        self.assertEqual(self.stored(), ["sh2007005138"])
        site = Site()
        self.fetch(site)
        self.assertEqual(site.calls[0], "sh85076432")      # picks up where it stopped
        self.assertEqual(self.stored(), IDS)

    def test_server_hiccup_pauses_and_retries(self):
        self.fetch(Site({"sh85076432": [503, 0]}))
        self.assertEqual(self.stored(), IDS)
        self.assertEqual(self.slept.count(harvest.PAUSE), 2)

    def test_waits_out_a_network_outage_without_giving_up(self):
        dns = (0, b"URLError(gaierror(-3, 'Temporary failure in name resolution'))")
        self.fetch(Site({"sh85076432": [dns] * 10}))
        self.assertEqual(self.stored(), IDS)
        self.assertEqual(self.slept.count(harvest.PAUSE), 10)

    def test_persistent_server_errors_stop(self):
        with self.assertRaisesRegex(harvest.Stop, "still failing"):
            self.fetch(Site({"sh85076432": [503] * (harvest.RETRIES + 1)}))
        with open(os.path.join(self.data, "dash", "status.json"), encoding="utf-8") as fh:
            s = json.load(fh)
        self.assertEqual(s["state"], "stopped")
        self.assertTrue(any("HTTP 503" in e for e in s["errors"]))

    def test_later_runs_fetch_only_what_changed(self):
        self.fetch()
        self.dump(1000, dates={"sh85148273": "2026-10-01T00:00:00"})
        site = Site()
        self.assertEqual(self.fetch(site), 1)
        self.assertEqual(site.calls, ["sh85148273"])

    def test_dashboard_counts_all_time_across_restarts(self):
        with self.assertRaises(harvest.Stop):
            self.fetch(Site({"sh85076432": 429}))        # stops after 1 stored
        with self.assertRaises(harvest.Stop):
            self.fetch(Site({"sh85148273": 429}))        # restart: continues from 2 stored
        with open(os.path.join(self.data, "dash", "status.json"), encoding="utf-8") as fh:
            s = json.load(fh)
        self.assertEqual((s["done"], s["total"]), (2, 1003))

    def test_dashboard_status_is_written(self):
        self.fetch()
        with open(os.path.join(self.data, "dash", "status.json"), encoding="utf-8") as fh:
            s = json.load(fh)
        self.assertEqual((s["state"], s["done"], s["total"]), ("fetched", 1003, 1003))
        self.assertIn("eta", s)


class BuildTests(Base):
    def test_refuses_to_build_while_incomplete(self):
        self.assertFalse(harvest.build(self.c, os.path.join(self.data, "release")))

    def test_release(self):
        self.fetch()
        out = os.path.join(self.data, "release")
        self.assertTrue(harvest.build(self.c, out, date="20261020"))
        with open(os.path.join(out, "lcsh-20261020.mrc"), "rb") as fh:
            got = [harvest.lccn(r) for r in pymarc.MARCReader(fh, to_unicode=True, force_utf8=True)]
        self.assertEqual(sorted(got), ["sh 85076432", "sh 85148273", "sh2007005138"])
        with open(os.path.join(out, "lcsh-20261020.mrcxml"), "rb") as fh:
            self.assertEqual(len(pymarc.parse_xml_to_array(fh)), 3)
        with open(os.path.join(out, "manifest-20261020.csv"), encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0], ["lccn", "005", "heading"])
        self.assertTrue(all(r[1] and r[2] for r in rows[1:]))
        with open(os.path.join(out, "SHA256SUMS-20261020"), encoding="utf-8") as fh:
            for line in fh.read().split("\n")[:-1]:
                digest, name = line.split("  ")
                with open(os.path.join(out, name), "rb") as f:
                    self.assertEqual(hashlib.sha256(f.read()).hexdigest(), digest)

    def test_record_without_005_or_010_still_builds(self):
        self.fetch()
        rec = pymarc.parse_xml_to_array(io.BytesIO(RECORDS["sh85076432"]))[0]
        rec.remove_fields("005", "010")
        self.c.execute("UPDATE records SET xml=? WHERE id='sh85076432'", (pymarc.record_to_xml(rec, namespace=True),))
        out = os.path.join(self.data, "release")
        self.assertTrue(harvest.build(self.c, out, date="20261020"))
        with open(os.path.join(out, "manifest-20261020.csv"), encoding="utf-8") as fh:
            self.assertIn(["", "", "Libel and slander"], list(csv.reader(fh)))

    def test_failed_build_leaves_no_files(self):
        self.fetch()
        out = os.path.join(self.data, "release")
        harvest.build(self.c, out, date="20261101")
        before = sorted(os.listdir(out))
        with mock.patch.object(harvest, "heading", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                harvest.build(self.c, out, date="20261108")
        self.assertEqual(sorted(os.listdir(out)), before)

    def test_second_release_lists_new_changed_and_deleted(self):
        self.fetch()
        out = os.path.join(self.data, "release")
        kept = self.c.execute("SELECT changed FROM census WHERE id='sh2007005138'").fetchone()[0]
        self.c.execute("DELETE FROM census WHERE id='sh2007005138'")       # not in the first release
        harvest.build(self.c, out, date="20261101")
        self.assertFalse(os.path.exists(os.path.join(out, "changes-20261101.csv")))   # nothing to compare yet
        self.c.execute("INSERT INTO census VALUES('sh2007005138', ?)", (kept,))        # -> new
        self.c.execute("DELETE FROM census WHERE id='sh85076432'")                     # -> deleted
        body = self.c.execute("SELECT xml FROM records WHERE id='sh85148273'").fetchone()[0]
        body = re.sub(rb'(tag="005">)[^<]*', rb"\g<1>20261105000000.0", body)
        self.c.execute("UPDATE records SET xml=? WHERE id='sh85148273'", (body,))      # -> changed
        harvest.build(self.c, out, date="20261108")
        with open(os.path.join(out, "changes-20261108.csv"), encoding="utf-8") as fh:
            changes = [(r[0], r[1]) for r in list(csv.reader(fh))[1:]]
        self.assertEqual(changes, [("sh 85148273", "changed"), ("sh2007005138", "new")])
        with open(os.path.join(out, "deleted-20261108.csv"), encoding="utf-8") as fh:
            self.assertEqual(list(csv.reader(fh))[1:], [["sh 85076432", "Libel and slander"]])
        with open(os.path.join(out, "changes-20261108.mrc"), "rb") as fh:
            self.assertEqual(len(list(pymarc.MARCReader(fh, to_unicode=True, force_utf8=True))), 2)
        with open(os.path.join(out, "SHA256SUMS-20261108"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().split("\n")[:-1]), 7)

    def test_round_trip_is_faithful(self):
        """The .mrc holds exactly what LC sent: same fields, order, values. Only the
        leader's computed positions (record length 00-04, base address 12-16) may differ."""
        self.fetch()
        out = os.path.join(self.data, "release")
        harvest.build(self.c, out, date="20261020")
        src = {harvest.lccn(r): r for r in (pymarc.parse_xml_to_array(io.BytesIO(b))[0] for b in RECORDS.values())}
        with open(os.path.join(out, "lcsh-20261020.mrc"), "rb") as fh:
            for b in pymarc.MARCReader(fh, to_unicode=True, force_utf8=True):
                a = src[harvest.lccn(b)]
                la, lb = str(a.leader), str(b.leader)
                self.assertEqual(la[5:12] + la[17:], lb[5:12] + lb[17:])
                self.assertEqual([str(f) for f in a.get_fields()], [str(f) for f in b.get_fields()])


def make_release(data, date):
    """Used by the load/publish tests: a complete store and release in `data`."""
    c = harvest.connect(data)
    rows = [(i, "2025-08-04T00:00:00") for i in IDS] + [(f"sh9{n:07d}", "2020") for n in range(1000)]
    write_dump(os.path.join(data, "subjects.madsrdf.jsonld.gz"), rows)
    harvest.census(c, data, fetch_dump=None)
    harvest.fetch(c, data, getter=Site(), sleep=lambda s: None)
    harvest.build(c, os.path.join(data, "release"), date=date)
    c.close()


if __name__ == "__main__":
    unittest.main()
