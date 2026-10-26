"""The deterministic join that replaces asking a model for facts it was given.

The forward check in ``verify`` measured 3 of 16 constraints surviving a real
index. These tests pin the alternative: the constraint comes from the manifest
record, so the ratio stops depending on the model at all.
"""

import re
import tempfile
import unittest
from pathlib import Path

from provenance import stitch, verify
from provenance.models import Layer, Package, RepoFacts

#: The template every run's settings.yaml is generated from. Read rather than
#: restated, because a copy here would go stale exactly the way
#: ``DEPENDENCY_TYPE = "dependency"`` did.
SETTINGS_TEMPLATE = Path(__file__).resolve().parents[1] / "assets/graphrag/settings.yaml.in"


def configured_types():
    text = SETTINGS_TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"^\s*entity_types:\s*\[([^\]]*)\]", text, re.MULTILINE)
    if not match:
        raise AssertionError(f"no entity_types list in {SETTINGS_TEMPLATE}")
    return [t.strip() for t in match.group(1).split(",") if t.strip()]


CONFIGURED_TYPES = configured_types()


def package(name, constraint, source_file="requirements.txt", declared_line=None, **kw):
    return Package(
        name=name,
        ecosystem=kw.pop("ecosystem", "pypi"),
        layer=kw.pop("layer", Layer.PACKAGE),
        source_file=source_file,
        constraint=constraint,
        declared_line=declared_line,
        **kw,
    )


def frame(rows):
    """An entity table with the three columns the stitch reads."""
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "type", "description"])


class TestSourceOrderedConstraint(unittest.TestCase):
    """The parser sorts specifier clauses; the file does not.

    A reader opening requirements/default.txt finds ``>=2.1.8,<2.2.0``, so that
    is what belongs in the graph even though the parsed constraint reads
    ``<2.2.0,>=2.1.8``.
    """

    CASES = [
        # (name, constraint, declared_line, expected)
        ("kombu", "<2.2.0,>=2.1.8", "kombu>=2.1.8,<2.2.0", ">=2.1.8,<2.2.0"),
        ("mock", ">=0.7.0", "mock>=0.7.0", ">=0.7.0"),
        ("multiprocessing", "==2.6.2.1", "multiprocessing==2.6.2.1", "==2.6.2.1"),
        # Case differs between the line and the parsed name.
        ("Django", ">=1.3", "django>=1.3", ">=1.3"),
        # No declared line to recover ordering from; the parsed form stands.
        ("celery", "<2.2.0,>=2.1.8", None, "<2.2.0,>=2.1.8"),
        # An extras suffix is part of what was declared and is not stripped.
        ("celery", ">=3.0", "celery[redis]>=3.0", ">=3.0"),
    ]

    def test_the_declared_ordering_is_recovered(self):
        for name, constraint, line, expected in self.CASES:
            with self.subTest(name=name, line=line):
                got = stitch.source_ordered_constraint(
                    package(name, constraint, declared_line=line)
                )
                self.assertEqual(got, expected)


class TestVersionText(unittest.TestCase):
    def test_one_declaration_is_written_bare(self):
        packages = [package("kombu", "<2.2.0,>=2.1.8", declared_line="kombu>=2.1.8,<2.2.0")]
        self.assertEqual(stitch.version_text(packages), ">=2.1.8,<2.2.0")

    def test_the_same_constraint_in_several_files_is_written_once(self):
        packages = [
            package("mock", ">=0.7.0", "requirements/test.txt", "mock>=0.7.0"),
            package("mock", ">=0.7.0", "requirements/dev.txt", "mock>=0.7.0"),
        ]
        self.assertEqual(stitch.version_text(packages), ">=0.7.0")

    def test_differing_constraints_are_both_reported_with_their_files(self):
        # celery declares python-dateutil twice, differently. Collapsing that
        # to one value would assert something no manifest says.
        packages = [
            package("python-dateutil", ">=1.5,<2.0", "requirements/default.txt",
                    "python-dateutil>=1.5,<2.0"),
            package("python-dateutil", ">=2.0", "requirements/default-py3k.txt",
                    "python-dateutil>=2.0"),
        ]
        text = stitch.version_text(packages)
        self.assertIn(">=1.5,<2.0 (requirements/default.txt)", text)
        self.assertIn(">=2.0 (requirements/default-py3k.txt)", text)

    def test_no_constraint_yields_no_field(self):
        self.assertIsNone(stitch.version_text([package("kombu", None)]))


class TestDescriptionRewriting(unittest.TestCase):
    """Idempotency, which is what makes the stage safe to re-run."""

    def test_a_field_is_appended_to_prose(self):
        got = stitch._with_version("Messaging library used by the worker.", ">=2.1.8")
        self.assertEqual(got, "Messaging library used by the worker. Version: >=2.1.8")

    def test_missing_terminal_punctuation_is_supplied(self):
        got = stitch._with_version("Messaging library", ">=2.1.8")
        self.assertEqual(got, "Messaging library. Version: >=2.1.8")

    def test_rewriting_twice_changes_nothing_the_second_time(self):
        once = stitch._with_version("Messaging library.", ">=2.1.8")
        self.assertEqual(stitch._with_version(once, ">=2.1.8"), once)

    def test_a_models_own_version_field_is_replaced_not_duplicated(self):
        got = stitch._with_version("Messaging library. Version: >=2.1.8", ">=2.1.8,<2.2.0")
        self.assertEqual(got, "Messaging library. Version: >=2.1.8,<2.2.0")
        self.assertEqual(got.count("Version:"), 1)

    def test_an_empty_description_still_gets_the_field(self):
        self.assertEqual(stitch._with_version("", ">=2.1.8"), "Version: >=2.1.8")

    def test_a_mangled_repository_sentence_is_corrected(self):
        got = stitch._with_repository(
            "KOMBU is a messaging library, hosted on GitHub in the celery repository. "
            "Repository hosted at celery.",
            stitch.repository_sentence("https://github.com/celery/celery.git"),
        )
        self.assertTrue(got.endswith("Repository hosted at https://github.com/celery/celery.git."))
        self.assertEqual(got.count("Repository hosted at"), 1)

    def test_a_runtime_sentence_survives_a_version_number_in_it(self):
        # "Python 2.7" contains a period, so a pattern that stops at the first
        # one strands "7." at the front of the description and the rewrite
        # stops being idempotent.
        sentence = stitch.repository_sentence(
            "https://github.com/celery/celery.git", "celery-v2-5-5", "Python 2.7"
        )
        once = stitch._with_repository("CELERY is the task queue.", sentence)
        self.assertEqual(once, "CELERY is the task queue. Repository hosted at "
                               "https://github.com/celery/celery.git, slug "
                               "celery-v2-5-5. Runtime stack: Python 2.7.")
        self.assertEqual(stitch._with_repository(once, sentence), once)


class TestStitchEntities(unittest.TestCase):
    FACTS = RepoFacts(
        root="/repo",
        git_repo="https://github.com/celery/celery.git",
        git_slug="celery",
        own_modules=["celery"],
        packages=[
            package("kombu", "<2.2.0,>=2.1.8", "requirements/default.txt",
                    "kombu>=2.1.8,<2.2.0"),
            package("mock", ">=0.7.0", "requirements/test.txt", "mock>=0.7.0"),
            package("multiprocessing", "==2.6.2.1", "requirements/py25.txt",
                    "multiprocessing==2.6.2.1"),
            package("nose", None, "requirements/test.txt", "nose"),
            package("absent", ">=1.0", "requirements/test.txt", "absent>=1.0"),
            package("glibc", "==2.34", "var/lib/rpm", ecosystem="rpm", layer=Layer.OS),
        ],
    )

    ROWS = [
        # The real failure modes, taken from the celery 2.5.5 index.
        ("CELERY", "REPOSITORY", "CELERY is the task queue. Repository hosted at celery."),
        ("KOMBU", "dependency", "KOMBU is a third-party messaging library."),
        ("MOCK", "dependency",
         "MOCK is listed in the requirements with a constraint of at least 0.7.0."),
        ("MULTIPROCESSING", "stdlib_module",
         "Standard library module providing process-based parallelism."),
        ("NOSE", "dependency", "Test runner."),
        ("celery.worker.state", "module", "Worker state tracking."),
    ]

    def setUp(self):
        self.frame, self.report = stitch.stitch_entities(self.FACTS, frame(self.ROWS))
        self.by_title = dict(zip(self.frame["title"], self.frame["description"], strict=True))

    def test_the_declared_ordering_reaches_the_graph(self):
        self.assertTrue(self.by_title["KOMBU"].endswith("Version: >=2.1.8,<2.2.0"))

    def test_a_paraphrased_constraint_gains_the_authoritative_field(self):
        # The prose stays; detecting every rephrasing is the unreliable problem
        # this module exists to avoid. The checkable field is what is added.
        self.assertIn("at least 0.7.0", self.by_title["MOCK"])
        self.assertTrue(self.by_title["MOCK"].endswith("Version: >=0.7.0"))

    def test_a_misclassified_entity_is_still_stitched(self):
        # multiprocessing==2.6.2.1 is a declared backport typed as stdlib. The
        # join goes by title, so the version lands anyway and the wrong type
        # is recorded rather than used as a reason to skip.
        self.assertTrue(self.by_title["MULTIPROCESSING"].endswith("Version: ==2.6.2.1"))
        change = next(c for c in self.report.changes
                      if c.title == "MULTIPROCESSING" and c.field_name == "version")
        self.assertEqual(change.entity_type, "stdlib_module")

    def test_a_manifest_entry_settles_the_layer_a_stdlib_name_suggests(self):
        # multiprocessing is in sys.stdlib_module_names on every interpreter
        # the model knows, and in celery 2.5.5 it is a pinned Python 2.5
        # backport. The manifest was read; the type is corrected, not reported.
        types = dict(zip(self.frame["title"], self.frame["type"], strict=True))
        self.assertEqual(types["MULTIPROCESSING"], "library")
        change = next(c for c in self.report.changes if c.field_name == "type")
        self.assertEqual(change.source, "requirements/py25.txt")

    def test_the_corrected_type_is_one_the_run_configures(self):
        # The type written here used to be the flat "dependency", which no
        # run has configured since it split into library and stdlib_module.
        # Every retyped entity carried a type nothing downstream reads.
        self.assertIn(stitch.DEPENDENCY_TYPE, CONFIGURED_TYPES)

    def test_a_type_the_manifest_does_not_contradict_is_left_alone(self):
        # KOMBU is declared too, and is already typed dependency. Retyping
        # anything that merely shares a distribution's name would invent one
        # defect to fix another.
        types = dict(zip(self.frame["title"], self.frame["type"], strict=True))
        self.assertEqual(types["KOMBU"], "dependency")
        self.assertEqual([c.title for c in self.report.changes
                          if c.field_name == "type"], ["MULTIPROCESSING"])

    def test_the_repository_url_is_written_in_full(self):
        self.assertIn("https://github.com/celery/celery.git", self.by_title["CELERY"])

    def test_a_declaration_without_a_constraint_is_left_alone(self):
        self.assertEqual(self.by_title["NOSE"], "Test runner.")

    def test_unrelated_entities_are_untouched(self):
        self.assertEqual(self.by_title["celery.worker.state"], "Worker state tracking.")

    def test_os_layer_packages_are_not_stitched_into_the_package_layer(self):
        self.assertNotIn("glibc", [c.title.lower() for c in self.report.changes])

    def test_a_declared_constraint_with_no_entity_is_reported_not_invented(self):
        self.assertEqual(len(self.report.unmatched), 1)
        self.assertIn("absent", self.report.unmatched[0])

    def test_the_input_frame_is_not_mutated(self):
        original = frame(self.ROWS)
        stitch.stitch_entities(self.FACTS, original)
        self.assertEqual(original.at[1, "description"], "KOMBU is a third-party messaging library.")

    def test_a_multi_repo_index_gets_versions_but_no_repository_url(self):
        # These facts describe one checkout. In a graph spanning several
        # repositories there is no way to tell which repository entity the
        # remote belongs to, and writing it to all of them would attach the
        # wrong URL to every repository but one.
        facts = RepoFacts(
            root=self.FACTS.root,
            git_repo=self.FACTS.git_repo,
            multi_repo=True,
            packages=self.FACTS.packages,
        )
        table, report = stitch.stitch_entities(facts, frame(self.ROWS))
        fields = {c.field_name for c in report.changes}
        # Versions and types are facts about a manifest and survive the merge;
        # the remote is the one fact that cannot be placed.
        self.assertEqual(fields, {"version", "type"})
        self.assertNotIn("github.com", table.at[0, "description"])
        self.assertTrue(any("several repositories" in s for s in report.skipped))

    def test_running_the_stitch_again_changes_nothing(self):
        second, report = stitch.stitch_entities(self.FACTS, self.frame)
        self.assertEqual(report.changes, [])
        self.assertEqual(
            list(second["description"]), list(self.frame["description"])
        )


class TestOffVocabulary(unittest.TestCase):
    """Types the run never offered, counted rather than corrected.

    On the restored celery run 72 of 651 entities carry a type outside
    ``entity_types``: 67 with no type at all and 5 typed ``variable``. Nothing
    downstream reads either, and nothing had been counting them.
    """

    #: Distinguishes "the caller did not say" from "the caller passed None",
    #: which is the case one of these tests is about.
    UNSET = object()

    def counts(self, rows, entity_types=UNSET):
        _frame, report = stitch.stitch_entities(
            RepoFacts(root="/repo"), frame(rows),
            CONFIGURED_TYPES if entity_types is self.UNSET else entity_types,
        )
        return report.off_vocabulary

    def test_a_blank_type_is_counted_not_hidden(self):
        # The commonest case by a wide margin, and the easiest to overlook
        # because an empty string reads as "nothing to report".
        self.assertEqual(self.counts([("CELERY.APP.BASE.APP", "", "")]), {"": 1})

    def test_a_type_outside_the_configured_list_is_counted(self):
        self.assertEqual(
            self.counts([("CELERYD_CONCURRENCY", "variable", "")]), {"variable": 1}
        )

    def test_configured_types_are_silent(self):
        rows = [(t.upper(), t, "") for t in CONFIGURED_TYPES]
        self.assertEqual(self.counts(rows), {})

    def test_the_types_the_join_writes_itself_are_exempt(self):
        # REPOSITORY is created by provenance.graph from the header slug, not
        # asked for, so holding it to the prompt's vocabulary would report a
        # defect this pipeline committed on purpose.
        self.assertEqual(self.counts([("CELERY-V2-5-5", "REPOSITORY", "")]), {})

    def test_counts_are_reported_per_type(self):
        rows = [
            ("A", "", ""), ("B", "", ""), ("C", "variable", ""), ("D", "module", ""),
        ]
        counts = self.counts(rows)
        self.assertEqual(counts, {"": 2, "variable": 1})
        self.assertEqual(sum(counts.values()), 3)

    def test_without_a_vocabulary_the_check_is_skipped_not_guessed(self):
        # A default list here would be a second copy of settings.yaml.in, and
        # the wrong copy would report every entity as off-vocabulary.
        self.assertEqual(self.counts([("A", "variable", "")], entity_types=None), {})
        self.assertEqual(self.counts([("A", "variable", "")], entity_types=[]), {})

    def test_the_count_reaches_the_report_dict(self):
        _frame, report = stitch.stitch_entities(
            RepoFacts(root="/repo"), frame([("A", "variable", "")]), CONFIGURED_TYPES,
        )
        self.assertEqual(report.to_dict()["off_vocabulary"], {"variable": 1})
        self.assertIn("off-vocabulary: 1", stitch.render(report))


class TestVerifyAfterStitch(unittest.TestCase):
    """The point of the whole exercise, measured the way the defect was measured."""

    FACTS = TestStitchEntities.FACTS
    ROWS = TestStitchEntities.ROWS

    def ratio(self, table):
        _, stats = verify.check_declared_constraints(self.FACTS, table)
        return stats["constraints_survived"], stats["declared_constraints"]

    def test_the_model_output_loses_most_constraints(self):
        survived, declared = self.ratio(frame(self.ROWS))
        self.assertEqual(declared, 4)   # kombu, mock, multiprocessing, absent
        self.assertEqual(survived, 0)

    def test_after_stitching_every_present_entity_carries_its_constraint(self):
        stitched, _ = stitch.stitch_entities(self.FACTS, frame(self.ROWS))
        survived, declared = self.ratio(stitched)
        # 3 of 4: 'absent' has no entity, which no join can repair and which
        # the stitch report names rather than papering over.
        self.assertEqual((survived, declared), (3, 4))

    def test_the_git_url_check_passes_after_stitching(self):
        stitched, _ = stitch.stitch_entities(self.FACTS, frame(self.ROWS))
        self.assertEqual(verify.check_git_identity(self.FACTS, stitched), [])


class TestApply(unittest.TestCase):
    """The filesystem half: backups, idempotency, and the embedding warning."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name).resolve() / "output"
        self.output.mkdir()
        self.path = self.output / "entities.parquet"
        frame(TestStitchEntities.ROWS).to_parquet(self.path, index=False)
        self.facts = TestStitchEntities.FACTS

    def read(self):
        import pandas as pd

        table = pd.read_parquet(self.path)
        return dict(zip(table["title"], table["description"], strict=True))

    def test_a_missing_table_names_the_path_it_looked_for(self):
        with self.assertRaises(FileNotFoundError) as caught:
            stitch.apply(self.facts, self.output.parent)
        self.assertIn("entities.parquet", str(caught.exception))

    def test_the_table_is_rewritten_in_place(self):
        report = stitch.apply(self.facts, self.output)
        self.assertEqual(len(report.changes), 5)
        self.assertTrue(self.read()["KOMBU"].endswith("Version: >=2.1.8,<2.2.0"))

    def test_the_pre_stitch_table_is_kept(self):
        stitch.apply(self.facts, self.output)
        import pandas as pd

        original = pd.read_parquet(self.output / "entities.parquet.orig")
        self.assertEqual(
            original.at[1, "description"], "KOMBU is a third-party messaging library."
        )

    def test_a_second_run_neither_changes_the_table_nor_clobbers_the_backup(self):
        stitch.apply(self.facts, self.output)
        before = self.path.read_bytes()
        backup = (self.output / "entities.parquet.orig").read_bytes()
        report = stitch.apply(self.facts, self.output)
        self.assertEqual(report.changes, [])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.output / "entities.parquet.orig").read_bytes(), backup)

    def test_a_dry_run_reports_without_writing(self):
        before = self.path.read_bytes()
        report = stitch.apply(self.facts, self.output, dry_run=True)
        self.assertEqual(len(report.changes), 5)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.output / "entities.parquet.orig").exists())

    def test_a_stitch_declares_the_entity_vector_store_stale(self):
        lancedb = self.output / "lancedb"
        lancedb.mkdir()
        (lancedb / "default-entity-description.lance").mkdir()
        (lancedb / "default-text_unit-text.lance").mkdir()
        report = stitch.apply(self.facts, self.output, dry_run=True)
        self.assertEqual(report.stale_embeddings, ["lancedb/default-entity-description.lance"])
        self.assertIn("stale", stitch.render(report))

    def test_no_changes_means_no_stale_embedding_claim(self):
        stitch.apply(self.facts, self.output)
        (self.output / "lancedb").mkdir()
        (self.output / "lancedb" / "default-entity-description.lance").mkdir()
        report = stitch.apply(self.facts, self.output)
        self.assertEqual(report.stale_embeddings, [])


if __name__ == "__main__":
    unittest.main()
