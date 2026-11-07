import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import archive  # noqa: E402


class ArchiveTests(unittest.TestCase):
    def test_one_item_per_release_with_clear_metadata(self):
        r = {"date": "20261107", "count": 468352, "new": 150, "changed": 1, "deleted": 1, "files": []}
        self.assertEqual(archive.identifier(r["date"]), "lcsh-mirror-20261107")
        m = archive.metadata(r)
        self.assertEqual(m["date"], "2026-11-07")
        self.assertEqual(m["mediatype"], "data")
        self.assertIn("publicdomain/zero", m["licenseurl"])
        self.assertIn("468,352 records", m["description"])
        self.assertIn("150 new, 1 changed, 1 deleted", m["description"])
        self.assertIn("Unofficial", m["description"])

    def test_first_release_has_no_change_counts(self):
        m = archive.metadata({"date": "20261106", "count": 468203, "files": []})
        self.assertNotIn("changes since", m["description"])


if __name__ == "__main__":
    unittest.main()
