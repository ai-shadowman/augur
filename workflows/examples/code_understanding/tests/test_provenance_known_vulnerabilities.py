"""The deterministic block appended to the Known Vulnerabilities section."""

import datetime as dt
import unittest

from provenance import known_vulnerabilities as kv
from provenance.eol import SOURCE, Table
from provenance.models import Layer, Package
from provenance.vulns import AffectedRange, Database

TODAY = dt.date(2026, 10, 24)
DATABASE = Database(built="2026-10-23T06:30:15Z", schema="v6.1.10", ranges={
    ("python", "kombu"): (AffectedRange("GHSA-aaaa-bbbb-cccc", "<2.2.0", "fixed", "2.2.0",
                                        ("CVE-2020-1234",)),),
    ("python", "anyjson"): (AffectedRange("GHSA-dddd-ffff-gggg", "<0.1", "fixed", "0.1"),),
})
TABLE = Table(retrieved="2026-10-24", source=SOURCE, products={
    "python": {"label": "Python (upstream)", "names": ["python"], "cycles": {"2.7": "2020-01-01"}},
})


def package(name, constraint=None, ecosystem="pypi"):
    return Package(name=name, ecosystem=ecosystem, layer=Layer.PACKAGE,
                   source_file="requirements/default.txt", constraint=constraint,
                   declared_line=f"{name}{constraint or ''}")


PACKAGES = [package("anyjson", ">=0.3"), package("kombu"), package("kombu", ">=2.1.8,<2.2.0"),
            package("rake", ecosystem="gem")]


class TestAdvisories(unittest.TestCase):
    def setUp(self):
        self.text, self.cited = kv.advisories(PACKAGES, DATABASE)

    def test_every_declaration_has_a_row_affected_first(self):
        rows = [line for line in self.text.splitlines() if line.startswith("| `")]
        self.assertEqual([r.split("|")[3].strip() for r in rows],
                         ["all-affected", "declined", "unbounded", "none-affected"])

    def test_names_the_database_build_and_the_counts(self):
        self.assertIn("schema v6.1.10, built 2026-10-23T06:30:15Z", self.text)
        self.assertIn("4 declarations: 1 all-affected, 0 some-affected, 1 declined, "
                      "1 unbounded, 1 none-affected.", self.text)

    def test_cites_the_matched_advisory_and_its_aliases_only(self):
        self.assertEqual(self.cited, {"GHSA-aaaa-bbbb-cccc", "CVE-2020-1234"})
        self.assertIn("GHSA-aaaa-bbbb-cccc (CVE-2020-1234): affected `<2.2.0`, fixed in 2.2.0",
                      self.text)
        self.assertNotIn("GHSA-dddd", self.text)

    def test_an_unbounded_row_counts_the_advisories_it_cannot_judge(self):
        self.assertIn("| `kombu` | `requirements/default.txt` | unbounded | no version constraint "
                      "declared, so any version may install; 1 advisory known for the package |",
                      self.text)

    def test_missing_inputs_say_so(self):
        for packages, database, reason in ((None, DATABASE, "no dependency facts"),
                                           (PACKAGES, None, "no advisory snapshot")):
            with self.subTest(reason):
                text, cited = kv.advisories(packages, database)
                self.assertIn(f"Not assessed: {reason}", text)
                self.assertEqual(cited, set())


class TestEndOfLife(unittest.TestCase):
    def test_cites_the_table_and_its_date(self):
        text = kv.end_of_life("Python 2.7 / PostgreSQL 9.6", TABLE, TODAY)
        self.assertIn("retrieved 2026-10-24 from endoflife.date", text)
        self.assertIn("| Python 2.7 | Python (upstream) | 2020-01-01 | ended |", text)
        self.assertIn("| PostgreSQL 9.6 |  |  | not in table |", text)

    def test_no_table_says_so(self):
        self.assertIn("Not assessed: the lifecycle table could not be read",
                      kv.end_of_life("Python 2.7", None, TODAY))

    def test_no_runtime_says_so(self):
        self.assertIn("Not assessed: the document headers record no runtime",
                      kv.end_of_life(None, TABLE, TODAY))


class TestUnverified(unittest.TestCase):
    CASES = [
        ("Nothing cited.", []),
        ("See CVE-2020-1234 and ghsa-aaaa-bbbb-cccc.", []),
        ("CVE-2011-4356 affects kombu; CVE-2011-4356 again; PYSEC-2021-9.",
         ["CVE-2011-4356", "PYSEC-2021-9"]),
    ]

    def test_cases(self):
        cited = {"GHSA-aaaa-bbbb-cccc", "CVE-2020-1234"}
        for answer, expected in self.CASES:
            with self.subTest(answer):
                self.assertEqual(kv.unverified(answer, cited), expected)

    def test_render_flags_a_model_cited_id(self):
        block = kv.render("Django has CVE-2019-0001.", PACKAGES, DATABASE, "Python 2.7",
                          TABLE, TODAY)
        self.assertIn("names CVE-2019-0001, which the advisory join did not produce", block)
        self.assertNotIn("did not produce", kv.render("Clean.", PACKAGES, DATABASE, None,
                                                      TABLE, TODAY))


if __name__ == "__main__":
    unittest.main()
