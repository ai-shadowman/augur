import json
import unittest
from unittest import mock

from provenance import vulns
from provenance.models import Layer, Package
from provenance.vulns import AffectedRange, Database, Verdict

BUILT = "2026-10-23T06:30:15Z"


def _package(name="kombu", constraint=None, version=None, ecosystem="pypi"):
    line = f"{name}{constraint or ''}"
    return Package(
        name=name,
        ecosystem=ecosystem,
        layer=Layer.PACKAGE,
        source_file="requirements/default.txt",
        constraint=constraint,
        version=version,
        declared_line=line,
    )


def _database(*constraints, name="kombu", fixed=None):
    affected = tuple(
        AffectedRange(advisory=f"GHSA-{i}", constraint=c, fix_state="fixed", fixed=fixed)
        for i, c in enumerate(constraints)
    )
    return Database(built=BUILT, schema="v6.1.10", ranges={("python", name): affected})


class TestVerdicts(unittest.TestCase):
    """Constructed ranges, boundaries included, against the verdict each earns."""

    CASES = [
        # (declared, advisory ranges, verdict)
        (">=2.1.8,<2.2.0", ["<2.2.0"], Verdict.ALL_AFFECTED),
        ("<2.2.0", [">=2.2.0,<2.3"], Verdict.NONE_AFFECTED),
        ("<=2.2.0", [">=2.2.0,<2.3"], Verdict.SOME_AFFECTED),
        (">=2.1.8,<2.2.0", ["<2.1.9"], Verdict.SOME_AFFECTED),
        ("==2.1.8", ["<2.1.9"], Verdict.ALL_AFFECTED),
        ("==2.1.9", ["<2.1.9"], Verdict.NONE_AFFECTED),
        (">=1.5,<2.0", ["<1.7", ">=1.7,<2.0"], Verdict.ALL_AFFECTED),
        (">=1.5,<2.0", ["<1.7", ">1.7,<2.0"], Verdict.SOME_AFFECTED),
        (">=2.0", ["<3"], Verdict.SOME_AFFECTED),
        (">=2.0", [">0"], Verdict.ALL_AFFECTED),
        (">=2.0", ["=1.9.3"], Verdict.NONE_AFFECTED),
        (">=2.0", [], Verdict.NONE_AFFECTED),
        (None, ["<3"], Verdict.UNBOUNDED),
        ("===2.1.8", ["<3"], Verdict.DECLINED),
        (">=2,<1", ["<3"], Verdict.DECLINED),
    ]

    def test_cases(self):
        for declared, advisories, expected in self.CASES:
            with self.subTest(declared=declared, advisories=advisories):
                finding = vulns.assess(_package(constraint=declared), _database(*advisories))
                self.assertEqual(finding.verdict, expected, finding)
                self.assertEqual(finding.db_built, BUILT)

    def test_matched_carries_only_intersecting_ranges(self):
        finding = vulns.assess(
            _package(constraint=">=2.1.8,<2.2.0"), _database("<2.1.9", ">=3.0", fixed="2.1.9")
        )
        self.assertEqual([m.constraint for m in finding.matched], ["<2.1.9"])
        self.assertEqual(finding.matched[0].fixed, "2.1.9")
        self.assertEqual(finding.advisories_known, 2)

    def test_resolved_version_is_joined_as_a_pin(self):
        finding = vulns.assess(_package(version="2.1.8"), _database("<2.1.9"))
        self.assertEqual(finding.verdict, Verdict.ALL_AFFECTED)
        self.assertEqual(finding.purl, "pkg:pypi/kombu@2.1.8")

    def test_bare_name_still_counts_advisories(self):
        finding = vulns.assess(_package(), _database("<3", "<4"))
        self.assertEqual((finding.verdict, finding.advisories_known), (Verdict.UNBOUNDED, 2))


class TestEvidenceLimits(unittest.TestCase):
    """Declining is an answer; an absent or unread record is never "safe"."""

    def test_unreadable_range_alone_declines_rather_than_clears(self):
        finding = vulns.assess(_package(constraint=">=2.0"), _database("<0.9-stable"))
        self.assertEqual(finding.verdict, Verdict.DECLINED)
        self.assertEqual(len(finding.unreadable), 1)

    def test_unreadable_range_beside_a_match_keeps_the_positive_claim(self):
        finding = vulns.assess(_package(constraint=">=2.0"), _database("<0.9-stable", "<3"))
        self.assertEqual(finding.verdict, Verdict.SOME_AFFECTED)
        self.assertEqual(len(finding.unreadable), 1)

    def test_unknown_name_is_none_affected_with_zero_known(self):
        finding = vulns.assess(_package("anyjson", ">=0.3.1"), _database("<3"))
        self.assertEqual((finding.verdict, finding.advisories_known), (Verdict.NONE_AFFECTED, 0))
        self.assertIn(f"no advisories known as of {BUILT}", vulns.render([finding], _database()))

    def test_uncovered_ecosystem_declines(self):
        finding = vulns.assess(_package(ecosystem="npm", constraint="<3"), _database("<3"))
        self.assertEqual(finding.verdict, Verdict.DECLINED)
        self.assertIn("npm", finding.reason)

    def test_assess_all_keeps_every_declaration_in_order(self):
        packages = [_package(), _package("x", "<1"), _package(ecosystem="gem")]
        findings = vulns.assess_all(packages, _database("<3"))
        self.assertEqual([f.package for f in findings], packages)


class TestNames(unittest.TestCase):
    CASES = ["python-dateutil", "python_dateutil", "Python.DateUtil", "PYTHON-DATEUTIL"]

    def test_pep503_spellings_meet(self):
        database = _database("<2.0", name="python-dateutil")
        for name in self.CASES:
            with self.subTest(name=name):
                finding = vulns.assess(_package(name, ">=1.5,<2.0"), database)
                self.assertEqual(finding.verdict, Verdict.ALL_AFFECTED)


#: ``grype db search -o json`` trimmed to the fields the parser reads: one
#: GHSA record with two ranges, and an NVD CPE record Grype's matcher ignores.
SEARCH = [
    {
        "vulnerability": {"id": "GHSA-aaaa", "severity": "high"},
        "packages": [
            {
                "package": {"name": "Flask_AppBuilder", "ecosystem": "python"},
                "namespace": "github:language:python",
                "detail": {
                    "cves": ["CVE-2020-1"],
                    "ranges": [{"version": {"constraint": "<1.2"},
                                "fix": {"version": "1.2", "state": "fixed"}}],
                },
            },
            {
                "package": {"name": "flask-appbuilder", "ecosystem": "python"},
                "namespace": "github:language:python",
                "detail": {"ranges": [{"version": {"constraint": ">=2.0,<2.1"},
                                       "fix": {"state": "not-fixed"}}]},
            },
        ],
    },
    {
        "vulnerability": {"id": "CVE-2020-1"},
        "packages": [
            {"cpe": "cpe:2.3:a:x:flask-appbuilder:*", "namespace": "nvd:cpe",
             "detail": {"ranges": [{"version": {"constraint": "< 1.2"}}]}},
        ],
    },
]


class TestParseSearch(unittest.TestCase):
    def test_indexes_ghsa_by_pep503_name_and_skips_cpe(self):
        index = vulns.parse_search(SEARCH, "python")
        self.assertEqual(list(index), [("python", "flask-appbuilder")])
        first, second = index[("python", "flask-appbuilder")]
        self.assertEqual(
            (first.advisory, first.constraint, first.fixed, first.aliases, first.severity),
            ("GHSA-aaaa", "<1.2", "1.2", ("CVE-2020-1",), "high"),
        )
        self.assertEqual((second.fixed, second.fix_state), (None, "not-fixed"))

    def test_withdrawn_advisory_is_not_joined(self):
        withdrawn = [{**SEARCH[0], "vulnerability": {"id": "GHSA-w", "status": "rejected"}}]
        self.assertEqual(vulns.parse_search(withdrawn, "python"), {})

    def test_nvd_language_record_is_joined(self):
        record = [{"vulnerability": {"id": "CVE-2020-11075", "status": "active"}, "packages": [
            {"package": {"name": "anchore-engine", "ecosystem": "python"},
             "namespace": "nvd:language:python",
             "detail": {"ranges": [{"version": {"constraint": "<0.7.3"}}]}}]}]
        database = Database(BUILT, "v6", vulns.parse_search(record, "python"))
        finding = vulns.assess(_package("anchore-engine", version="0.7.0"), database)
        self.assertEqual(finding.verdict, Verdict.ALL_AFFECTED)

    def test_rangeless_entry_can_decline_but_never_affect(self):
        record = [{"vulnerability": {"id": "CVE-2026-25076"}, "packages": [
            {"package": {"name": "x"}, "namespace": "nvd:language:python", "detail": {}}]}]
        index = vulns.parse_search(record, "python")
        self.assertIsNone(index[("python", "x")][0].constraint)
        finding = vulns.assess(_package("x", "==1.0"), Database(BUILT, "v6", index))
        self.assertEqual(finding.verdict, Verdict.DECLINED)


class TestLoadDatabase(unittest.TestCase):
    def test_missing_binary_says_how_to_install(self):
        with (
            mock.patch.object(vulns.shutil, "which", return_value=None),
            self.assertRaisesRegex(vulns.GrypeNotAvailable, "brew install grype"),
        ):
            vulns.load_database()

    def test_invalid_database_says_how_to_fetch(self):
        status = {"valid": False, "path": "/nowhere/vulnerability.db"}
        with (
            mock.patch.object(vulns.shutil, "which", return_value="/bin/grype"),
            mock.patch.object(vulns, "_grype", return_value=status),
            self.assertRaisesRegex(vulns.GrypeNotAvailable, "grype db update"),
        ):
            vulns.load_database()

    def test_unusable_output_is_a_clean_error(self):
        outcomes = [
            vulns.subprocess.TimeoutExpired("grype", 300),
            mock.Mock(returncode=0, stdout="not json", stderr=""),
            mock.Mock(returncode=0, stdout=json.dumps({"valid": True}), stderr=""),
        ]
        for outcome in outcomes:
            with (
                self.subTest(outcome=outcome),
                mock.patch.object(vulns.shutil, "which", return_value="/bin/grype"),
                mock.patch.object(vulns.subprocess, "run", side_effect=[outcome]),
                self.assertRaises(vulns.GrypeNotAvailable),
            ):
                vulns.load_database()

    def test_reads_status_then_search_without_auto_update(self):
        status = {"valid": True, "built": BUILT, "schemaVersion": "v6.1.10"}
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs["env"]["GRYPE_DB_AUTO_UPDATE"]))
            payload = status if argv[2] == "status" else SEARCH
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        with (
            mock.patch.object(vulns.shutil, "which", return_value="/bin/grype"),
            mock.patch.object(vulns.subprocess, "run", side_effect=fake_run),
        ):
            database = vulns.load_database()
        self.assertEqual(database.built, BUILT)
        self.assertEqual([c[0][1:3] for c in calls], [["db", "status"], ["db", "search"]])
        self.assertTrue(all(flag == "false" for _, flag in calls))
        self.assertEqual(len(database.lookup("python", "Flask-AppBuilder")), 2)


if __name__ == "__main__":
    unittest.main()
