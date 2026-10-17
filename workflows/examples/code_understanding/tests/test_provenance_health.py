import json
import tempfile
import unittest
from pathlib import Path

from provenance import health
from provenance.models import Layer, Package

#: A real veripak payload for kombu, trimmed to the fields summarize reads.
#: Recorded from `veripak check kombu -e python --versions 2.1.8 --json`.
KOMBU_PAYLOAD = {
    "package": "kombu",
    "ecosystem": "python",
    "versions_in_use": ["2.1.8"],
    "version": {"version": "5.6.2", "method": "registry_api"},
    "download": {"confirmed": True, "method": "pip_download_no_deps"},
    "eol": {
        "project_status": "active",
        "eol": False,
        "eol_date": None,
        "confidence": "medium",
    },
    "cves": {"total_count": 0, "high_critical_count": 0, "method": "cve_agent"},
    "replacement": None,
    "hitl_flags": [
        {
            "reason": "Dropped 1 unverified CVE(s): CVE-2022-30236. These CVE IDs "
            "could not be confirmed against OSV.dev/NVD for package 'kombu'."
        }
    ],
}


class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.summary = health.summarize(KOMBU_PAYLOAD)

    def test_registry_sourced_fields_are_carried(self):
        self.assertEqual(self.summary["latest_upstream"], "5.6.2")
        self.assertEqual(self.summary["latest_source"], "registry_api")
        self.assertTrue(self.summary["download_confirmed"])

    def test_agent_sourced_fields_are_carried_with_their_confidence(self):
        self.assertEqual(self.summary["project_status"], "active")
        self.assertEqual(self.summary["eol_confidence"], "medium")
        self.assertEqual(self.summary["cve_count"], 0)

    def test_field_provenance_separates_the_two_kinds_of_answer(self):
        provenance = self.summary["field_provenance"]
        self.assertEqual(provenance["version"], "registry")
        self.assertEqual(provenance["cves"], "agent-assisted")
        self.assertEqual(provenance["eol"], "agent-assisted")

    def test_veripaks_own_rejected_claims_travel_with_the_result(self):
        # veripak's agent proposed a CVE its verifier then refused. That
        # rejection is a fact about the answer and should not be dropped.
        self.assertEqual(len(self.summary["unverified_claims"]), 1)
        self.assertIn("CVE-2022-30236", self.summary["unverified_claims"][0])

    def test_a_sparse_payload_does_not_raise(self):
        summary = health.summarize({"package": "mystery", "ecosystem": "python"})
        self.assertEqual(summary["package"], "mystery")
        self.assertIsNone(summary["latest_upstream"])
        self.assertEqual(summary["unverified_claims"], [])
        self.assertEqual(summary["versions_in_use"], [])


class TestCache(unittest.TestCase):
    """Enrichment makes network calls, so a rerun must not repeat them.

    The cache-hit path never reaches the veripak binary, which is what makes an
    interrupted run resumable and keeps the free-tier NVD rate limit intact.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)

    def seed(self, payload, name="kombu", ecosystem="python", versions=("2.1.8",)):
        path = health._cache_path(self.cache, ecosystem, name, tuple(versions))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_seeded_result_is_returned_without_running_veripak(self):
        self.seed(KOMBU_PAYLOAD)
        result = health.check_package("kombu", versions=["2.1.8"], cache_dir=self.cache)
        self.assertEqual(result["version"]["version"], "5.6.2")

    def test_cache_key_folds_name_spelling_but_not_version(self):
        self.seed(KOMBU_PAYLOAD)
        self.assertTrue(
            health._cache_path(self.cache, "python", "Kombu", ("2.1.8",)).exists()
        )
        self.assertFalse(
            health._cache_path(self.cache, "python", "kombu", ("2.1.9",)).exists()
        )

    def test_unversioned_lookups_have_their_own_cache_slot(self):
        path = health._cache_path(self.cache, "python", "kombu", ())
        self.assertTrue(path.name.endswith("@any.json"))

    def test_enrich_collapses_declarations_to_distinct_distributions(self):
        self.seed(KOMBU_PAYLOAD)
        declarations = [
            Package(name="kombu", ecosystem="pypi", layer=Layer.PACKAGE,
                    source_file=f"requirements/{n}.txt", version="2.1.8")
            for n in ("default", "test", "py25")
        ]
        results = health.enrich(declarations, cache_dir=self.cache)
        self.assertEqual(list(results), ["kombu"])
        self.assertEqual(results["kombu"]["latest_upstream"], "5.6.2")

    def test_unknown_ecosystems_are_skipped_rather_than_guessed_at(self):
        rpm = Package(name="glibc", ecosystem="rpm", layer=Layer.OS,
                      source_file="var/lib/rpm", version="2.34")
        self.assertEqual(health.enrich([rpm], cache_dir=self.cache), {})

    def test_one_bad_result_is_recorded_without_sinking_the_run(self):
        self.seed(KOMBU_PAYLOAD)
        corrupt = health._cache_path(self.cache, "python", "broken", ())
        corrupt.parent.mkdir(parents=True, exist_ok=True)
        corrupt.write_text("{ truncated", encoding="utf-8")

        packages = [
            Package(name="kombu", ecosystem="pypi", layer=Layer.PACKAGE,
                    source_file="requirements.txt", version="2.1.8"),
            Package(name="broken", ecosystem="pypi", layer=Layer.PACKAGE,
                    source_file="requirements.txt"),
        ]
        with self.assertLogs("provenance.health", level="WARNING"):
            results = health.enrich(packages, cache_dir=self.cache)

        self.assertEqual(sorted(results), ["broken", "kombu"])
        self.assertEqual(results["kombu"]["latest_upstream"], "5.6.2")
        self.assertIn("error", results["broken"])


class TestEcosystemMap(unittest.TestCase):
    def test_purl_types_map_to_veripak_names(self):
        self.assertEqual(health.ECOSYSTEM_MAP["pypi"], "python")
        self.assertEqual(health.ECOSYSTEM_MAP["maven"], "java")

    def test_os_ecosystems_are_absent_so_they_are_skipped(self):
        for ecosystem in ("rpm", "deb", "apk"):
            self.assertNotIn(ecosystem, health.ECOSYSTEM_MAP)


if __name__ == "__main__":
    unittest.main()
