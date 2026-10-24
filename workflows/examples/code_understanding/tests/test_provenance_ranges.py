import unittest

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from provenance import ranges

#: Final releases only: the module departs from PEP 440 at pre- and
#: post-release boundaries by design, so agreement is asserted where it holds.
VERSIONS = [
    "0.9", "1.0", "1.0.1", "1.4.9", "1.5", "1.5.0.1", "1.5.3", "1.6", "1.9.9",
    "2.0", "2.1.7", "2.1.8", "2.1.9", "2.2.0", "2.2.1", "3.0", "10.0", "1!0.5",
]

SPECIFIERS = [
    ">=2.1.8,<2.2.0", "<2.2.0", "<=2.2.0", ">2.2.0", ">=2.0", "==1.5", "==1.5.*",
    "!=1.5.*", "!=2.1.8", "~=1.5", "~=1.5.0", ">=1.5,<2.0,!=1.6", "", "1!0.5",
]


def _member(version: str, versions: ranges.VersionSet) -> bool:
    point = ranges.parse(f"=={version}")
    return ranges.contains(versions, point)


class TestAgreesWithPackaging(unittest.TestCase):
    """On final releases, interval membership is exactly ``SpecifierSet``'s."""

    def test_membership_matches_specifier_set(self):
        for spec in SPECIFIERS:
            if spec == "1!0.5":
                spec = "==1!0.5"
            versions = ranges.parse(spec)
            for v in VERSIONS:
                with self.subTest(spec=spec, version=v):
                    expected = SpecifierSet(spec).contains(Version(v), prereleases=True)
                    self.assertEqual(_member(v, versions), expected)


class TestSetOperations(unittest.TestCase):
    CASES = [
        # (a, b, intersects, a contains b)
        (">=2.1.8,<2.2.0", "<2.2.0", True, False),
        ("<2.2.0", ">=2.1.8,<2.2.0", True, True),
        ("<2.2.0", ">=2.2.0", False, False),
        ("<=2.2.0", ">=2.2.0", True, False),
        ("==2.2.0", "==2.2.0", True, True),
        ("", ">=1.0", True, True),
        (">0", ">=2.0", True, True),
        ("!=1.6", ">=1.5,<2.0", True, False),
    ]

    def test_cases(self):
        for a, b, meets, holds in self.CASES:
            with self.subTest(a=a, b=b):
                sa, sb = ranges.parse(a), ranges.parse(b)
                self.assertEqual(ranges.intersects(sa, sb), meets)
                self.assertEqual(ranges.contains(sa, sb), holds)

    def test_union_closes_a_split_range_only_when_touching(self):
        whole = ranges.parse(">=1.5,<2.0")
        joined = ranges.union(ranges.parse("<1.7"), ranges.parse(">=1.7,<2.0"))
        holed = ranges.union(ranges.parse("<1.7"), ranges.parse(">1.7,<2.0"))
        self.assertTrue(ranges.contains(joined, whole))
        self.assertFalse(ranges.contains(holed, whole))
        self.assertEqual(len(joined), 1, joined)

    def test_complement_round_trips(self):
        for spec in SPECIFIERS:
            with self.subTest(spec=spec):
                versions = ranges.parse(spec.replace("1!0.5", "==1!0.5"))
                self.assertEqual(ranges.complement(ranges.complement(versions)), versions)

    def test_contradiction_is_empty(self):
        self.assertEqual(ranges.parse(">=2,<1"), ranges.NOTHING)


class TestGrypeSyntax(unittest.TestCase):
    CASES = [
        ("=1.9.3", "==1.9.3"),
        ("< 5.1", "<5.1"),
        (">= 1.0, < 2.0", ">=1.0,<2.0"),
        ("=0.1a0", "==0.1a0"),
    ]

    def test_reads_as_pep440(self):
        for grype, pep440 in self.CASES:
            with self.subTest(grype=grype):
                self.assertEqual(ranges.parse(grype), ranges.parse(pep440))


class TestUnsupported(unittest.TestCase):
    """A clause that cannot be read raises, naming it, rather than vanishing."""

    CASES = ["===foo", "<0.9-stable", "<0.8.3ubuntu7.5", ">=1.0,<garbage", "~=1", "lol"]

    def test_raises_with_the_clause(self):
        for spec in self.CASES:
            with self.subTest(spec=spec):
                with self.assertRaises(ranges.UnsupportedSpecifier) as ctx:
                    ranges.parse(spec)
                self.assertIn(spec, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
