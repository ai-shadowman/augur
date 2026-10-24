"""End-of-life lookups against the dated lifecycle table."""

import datetime as dt
import unittest

from provenance import eol
from provenance.eol import Status

TODAY = dt.date(2026, 10, 24)

TABLE = eol.Table(retrieved="2026-10-24", source=eol.SOURCE, products={
    "python": {"label": "Python (upstream)", "names": ["python", "cpython"],
               "cycles": {"2.7": "2020-01-01", "3.10": "2026-10-01", "3.12": "2028-10-31",
                          "3.16": False}},
    "java": {"label": "Red Hat build of OpenJDK", "names": ["java"],
             "versions": {"1.8": "8"}, "cycles": {"8": "2026-11-30", "6": True}},
})


class TestAssess(unittest.TestCase):
    CASES = [
        # (header component, status, cycle, eol)
        ("Python 2.7", Status.ENDED, "2.7", "2020-01-01"),
        ("Python 2.7.18", Status.ENDED, "2.7", "2020-01-01"),
        ("python 3.10", Status.ENDED, "3.10", "2026-10-01"),
        ("CPython 3.12", Status.SUPPORTED, "3.12", "2028-10-31"),
        ("Python 3.16", Status.NO_DATE, "3.16", None),
        ("Java 1.8", Status.SUPPORTED, "8", "2026-11-30"),
        ("Java 6", Status.ENDED, "6", None),
        ("Python 3.1", Status.UNKNOWN_VERSION, None, None),
        ("Ruby 2.7=end", Status.UNKNOWN_COMPONENT, None, None),
        ("Python 2.7=end", Status.UNKNOWN_VERSION, None, None),
        ("Python", Status.UNKNOWN_VERSION, None, None),
        ("PostgreSQL 9.6", Status.UNKNOWN_COMPONENT, None, None),
    ]

    def test_cases(self):
        for component, status, cycle, date in self.CASES:
            with self.subTest(component):
                a = TABLE.assess(component, TODAY)
                self.assertEqual((a.status, a.cycle, a.eol), (status, cycle, date), a)

    def test_the_end_date_itself_counts_as_ended(self):
        self.assertIs(TABLE.assess("Python 3.10", dt.date(2026, 10, 1)).status, Status.ENDED)
        self.assertIs(TABLE.assess("Python 3.10", dt.date(2026, 9, 30)).status, Status.SUPPORTED)


class TestComponents(unittest.TestCase):
    def test_splits_the_header_on_slashes(self):
        self.assertEqual(eol.components("Python 3.9 / Django 3.2 /  PostgreSQL 13"),
                         ["Python 3.9", "Django 3.2", "PostgreSQL 13"])
        self.assertEqual(eol.components(None), [])
        self.assertEqual(eol.components(""), [])


class TestCommittedTable(unittest.TestCase):
    """The asset the report cites: dated, covering every product it declares."""

    def test_is_dated_and_complete(self):
        table = eol.load()
        dt.date.fromisoformat(table.retrieved)
        self.assertEqual(set(table.products), set(eol.PRODUCTS))
        for key, product in table.products.items():
            with self.subTest(key):
                self.assertTrue(product["cycles"])
                self.assertEqual(product["names"], eol.PRODUCTS[key]["names"])

    def test_answers_the_runtimes_on_disk(self):
        # The three headers the measured corpora carry.
        table = eol.load()
        for component in ("Python 2.7", "Java 8", "Ruby 2.7"):
            with self.subTest(component):
                self.assertIsNotNone(table.assess(component, TODAY).eol)


if __name__ == "__main__":
    unittest.main()
