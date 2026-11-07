import gzip
import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import harvest  # noqa: E402
import publish  # noqa: E402
from tests.test_harvest import make_release  # noqa: E402


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.release = os.path.join(self.tmp.name, "release")
        self.out = os.path.join(self.tmp.name, "publish", "lcsh")

    def tearDown(self):
        self.tmp.cleanup()

    def make_release(self, date):
        make_release(self.tmp.name, date)

    def test_stage_writes_what_the_page_reads(self):
        self.make_release("20261024")
        publish.stage(self.release, self.out)
        with open(os.path.join(self.out, "latest.json"), encoding="utf-8") as fh:
            r = json.load(fh)
        self.assertEqual(r["date"], "20261024")
        self.assertEqual(r["count"], 3)
        self.assertEqual([f["name"] for f in r["files"]],
                         ["lcsh-20261024.mrc.gz", "lcsh-20261024.mrcxml.gz", "manifest-20261024.csv.gz"])
        for f in r["files"]:
            p = os.path.join(self.out, f["name"])
            self.assertEqual(os.path.getsize(p), f["size"])
            with open(p, "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), f["sha256"])
        # gz decompresses to the original, and latest copies match the dated ones
        with gzip.open(os.path.join(self.out, "lcsh-latest.mrc.gz"), "rb") as fh, \
                open(os.path.join(self.release, "lcsh-20261024.mrc"), "rb") as orig:
            self.assertEqual(fh.read(), orig.read())

    def test_keeps_only_the_newest_releases(self):
        dates = ["20261003", "20261010", "20261017", "20261024", "20261031"]
        for d in dates:
            self.make_release(d)
            publish.stage(self.release, self.out)
        staged = {f.split("-")[1][:8] for f in os.listdir(self.out) if f.startswith("lcsh-2")}
        self.assertEqual(staged, set(dates[-publish.KEEP:]))
        kept = {f.split("-")[1][:8] for f in os.listdir(self.release) if f.startswith("lcsh-")}
        self.assertEqual(kept, set(dates[-publish.KEEP:]))
        with open(os.path.join(self.out, "latest.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["date"], "20261031")
        with open(os.path.join(self.out, "releases.json"), encoding="utf-8") as fh:
            rel = json.load(fh)
        self.assertEqual([r["date"] for r in rel], sorted(dates[-publish.KEEP:], reverse=True))
        self.assertTrue(all(r["count"] == 3 and len(r["files"]) == 3 for r in rel))


if __name__ == "__main__":
    unittest.main()
