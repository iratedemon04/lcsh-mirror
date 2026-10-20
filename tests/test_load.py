import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import harvest  # noqa: E402
import load  # noqa: E402
from tests.test_harvest import GF, SH, Server  # noqa: E402

# the query tui.find() runs
FIND = ("SELECT f.lccn, r.heading FROM fts f JOIN records r USING(lccn) "
        "WHERE fts MATCH ? ORDER BY bm25(fts, 0, 10, 1) LIMIT 5000")


class LoadTests(unittest.TestCase):
    def test_release_becomes_searchable(self):
        with tempfile.TemporaryDirectory() as d:
            old, harvest.PAGE = harvest.PAGE, 2
            try:
                harvest.fetch(os.path.join(d, "pages"), Server())
            finally:
                harvest.PAGE = old
            harvest.build(os.path.join(d, "pages"), os.path.join(d, "release"), date="20261020")
            db = os.path.join(d, "lcsh.db")
            self.assertEqual(load.load(os.path.join(d, "release", "lcsh-20261020.mrc"), db), 3)
            c = load.connect(db)
            self.assertEqual(c.execute(FIND, ('"libel"*',)).fetchall(), [("sh 85076432", "Libel and slander")])
            self.assertEqual(c.execute(FIND, ('"calumny"*',)).fetchall()[0][0], "sh 85076432")  # see-ref
            self.assertEqual(c.execute(FIND, ('"journalism"*',)).fetchall(), [])              # gf excluded
            marc = c.execute("SELECT marc FROM records WHERE lccn='sh 85076432'").fetchone()[0]
            self.assertIn("=150  \\\\$aLibel and slander", load.preview(marc))
            c.close()


if __name__ == "__main__":
    unittest.main()
