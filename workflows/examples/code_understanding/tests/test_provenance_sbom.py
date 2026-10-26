import json
import tempfile
import unittest
from pathlib import Path

from provenance import sbom
from provenance.models import Layer

#: A syft-json document trimmed to the fields the converter reads, covering
#: one application package and one OS package.
SYFT_DOCUMENT = {
    "artifacts": [
        {
            "name": "kombu",
            "version": "2.1.8",
            "type": "python",
            "purl": "pkg:pypi/kombu@2.1.8",
            "locations": [{"path": "/requirements.txt"}],
        },
        {
            "name": "glibc",
            "version": "2.34-100.el9",
            "type": "rpm",
            "purl": "pkg:rpm/redhat/glibc@2.34-100.el9",
            "locations": [{"path": "/var/lib/rpm/rpmdb.sqlite"}],
        },
        {"version": "1.0", "type": "binary"},  # no name: nothing to record
    ]
}


class TestEcosystemDetection(unittest.TestCase):
    """The PURL type is canonical; syft's own type string is the fallback."""

    CASES = [
        ({"purl": "pkg:pypi/kombu@2.1.8", "type": "python"}, "pypi"),
        ({"purl": "pkg:rpm/redhat/glibc@2.34", "type": "rpm"}, "rpm"),
        ({"purl": "pkg:npm/lodash@4.17.21", "type": "npm"}, "npm"),
        ({"purl": "", "type": "Python"}, "python"),
        ({}, "unknown"),
    ]

    def test_purl_type_wins_over_the_type_string(self):
        for artifact, expected in self.CASES:
            with self.subTest(artifact=artifact):
                self.assertEqual(sbom._ecosystem_of(artifact), expected)


class TestArtifactConversion(unittest.TestCase):
    def setUp(self):
        self.packages = sbom.artifacts_to_packages(SYFT_DOCUMENT)
        self.by_name = {p.name: p for p in self.packages}

    def test_os_packages_land_on_the_os_layer(self):
        self.assertIs(self.by_name["glibc"].layer, Layer.OS)

    def test_language_packages_land_on_the_package_layer(self):
        self.assertIs(self.by_name["kombu"].layer, Layer.PACKAGE)

    def test_syft_reports_a_resolved_version_and_no_constraint(self):
        kombu = self.by_name["kombu"]
        self.assertEqual(kombu.version, "2.1.8")
        self.assertIsNone(kombu.constraint)

    def test_source_paths_are_recorded_without_a_leading_slash(self):
        self.assertEqual(self.by_name["kombu"].source_file, "requirements.txt")

    def test_an_artifact_without_a_name_is_skipped(self):
        self.assertEqual(len(self.packages), 2)

    def test_an_empty_document_yields_nothing(self):
        self.assertEqual(sbom.artifacts_to_packages({}), [])
        self.assertEqual(sbom.artifacts_to_packages({"artifacts": None}), [])


class TestSbomReuse(unittest.TestCase):
    """A cached SBOM is reused, so a rerun does not repeat the scan."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "sbom.json"
        self.path.write_text(json.dumps(SYFT_DOCUMENT), encoding="utf-8")

    def test_an_existing_document_is_read_instead_of_rescanning(self):
        packages = sbom.scan_directory(self.root, sbom_path=self.path)
        self.assertEqual(sorted(p.name for p in packages), ["glibc", "kombu"])

    def test_force_is_what_makes_a_rescan_happen(self):
        # With force set and no syft available the call must fail loudly
        # rather than quietly returning the stale document.
        if sbom.shutil.which("syft"):
            self.skipTest("syft is installed; the no-binary path cannot be exercised")
        with self.assertRaises(sbom.SyftNotAvailable):
            sbom.scan_directory(self.root, sbom_path=self.path, force=True)


@unittest.skipUnless(sbom.shutil.which("syft"), "syft is not installed")
class TestRealScan(unittest.TestCase):
    """Exercise syft itself. Offline, and small enough to stay fast.

    This also pins the two syft behaviours the module docstring records, since
    both are load-bearing for the decision to keep the manifest parsers.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # The checkout is a child of the temp directory, not the temp
        # directory itself, so "the parent" is somewhere these tests own and
        # siblings can be written without touching the system temp root.
        self.parent = Path(directory.name).resolve()
        self.root = self.parent / "repo"
        (self.root / "requirements").mkdir(parents=True)
        (self.root / "requirements.txt").write_text(
            "kombu==2.1.8\nanyjson>=0.3.1\n", encoding="utf-8"
        )
        (self.root / "requirements" / "py25.txt").write_text(
            "multiprocessing==2.6.2.1\n", encoding="utf-8"
        )

    def test_version_reports_something(self):
        self.assertTrue(sbom.syft_version())

    def test_exact_pins_are_cataloged_and_ranges_are_not(self):
        names = {p.name for p in sbom.scan_directory(self.root)}
        self.assertIn("kombu", names)
        self.assertNotIn("anyjson", names)

    def test_requirements_are_selected_by_filename_not_by_directory(self):
        # requirements/py25.txt does not match *requirements*.txt, so syft
        # never reads it. This is why celery 2.5.5 yields zero artifacts and
        # why the manifest parsers carry the Python package layer.
        names = {p.name for p in sbom.scan_directory(self.root)}
        self.assertNotIn("multiprocessing", names)

    def test_the_raw_document_is_cached_for_reuse(self):
        destination = self.parent / "sbom.json"
        sbom.scan_directory(self.root, sbom_path=destination)
        self.assertIn("artifacts", json.loads(destination.read_text(encoding="utf-8")))

    def test_a_scan_stays_inside_the_directory_it_was_given(self):
        # A relative path under a symlinked parent used to make syft walk the
        # parent and catalog unrelated trees. Paths are resolved before use.
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "requirements.txt").write_text("redis==2.4.4\n", encoding="utf-8")
        names = {p.name for p in sbom.scan_directory(self.root)}
        self.assertNotIn("redis", names)


if __name__ == "__main__":
    unittest.main()
