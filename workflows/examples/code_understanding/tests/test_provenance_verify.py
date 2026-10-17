import tempfile
import unittest
from pathlib import Path

from provenance import verify
from provenance.corpus import Corpus, CorpusFile
from provenance.models import (
    FindingKind,
    Layer,
    Package,
    RepoFacts,
    VerificationReport,
)


def entity_frame(rows):
    """Build the minimal entity table the checks read.

    pandas is imported here rather than at module scope: see tests/conftest.py
    for why a DataFrame must not be built while this module is being imported.
    """
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "type", "description"])


def package(name, constraint=None, declared_line=None, source_file="requirements.txt",
            layer=Layer.PACKAGE, ecosystem="pypi", version=None):
    return Package(
        name=name,
        ecosystem=ecosystem,
        layer=layer,
        source_file=source_file,
        version=version,
        constraint=constraint,
        declared_line=declared_line,
    )


CELERY_FACTS = RepoFacts(
    root="/tmp/celery-255",
    git_repo="https://github.com/celery/celery.git",
    git_slug="celery",
    own_modules=["celery"],
    packages=[
        package("kombu", "<2.2.0,>=2.1.8", "kombu>=2.1.8,<2.2.0"),
        package("python-dateutil", ">=1.5,<2.0", "python-dateutil>=1.5,<2.0"),
        package("anyjson", ">=0.3.1", "anyjson>=0.3.1"),
    ],
)


class TestGitIdentity(unittest.TestCase):
    """The repository URL is injected into every file header before indexing.

    Nothing about it requires judgement, so anything other than a byte-exact
    match in the finished graph is a defect.
    """

    def test_verbatim_url_and_slug_pass(self):
        entities = entity_frame([
            ("CELERY", "application", "Repository hosted at "
             "https://github.com/celery/celery.git, slug celery"),
        ])
        self.assertEqual(verify.check_git_identity(CELERY_FACTS, entities), [])

    def test_paraphrased_url_is_reported_as_mangled_not_missing(self):
        entities = entity_frame([
            ("CELERY", "application", "A project in the celery repository on GitHub"),
        ])
        findings = {f.subject: f for f in verify.check_git_identity(CELERY_FACTS, entities)}
        self.assertEqual(findings["git_repo"].kind, FindingKind.MANGLED)
        self.assertEqual(findings["git_repo"].expected,
                         "https://github.com/celery/celery.git")

    def test_absent_url_is_reported_as_missing(self):
        entities = entity_frame([("KOMBU", "library", "A messaging library")])
        kinds = {f.subject: f.kind for f in verify.check_git_identity(CELERY_FACTS, entities)}
        self.assertEqual(kinds["git_repo"], FindingKind.MISSING)
        self.assertEqual(kinds["git_slug"], FindingKind.MISSING)


class TestDeclaredConstraints(unittest.TestCase):
    """The defect that prompted this package: versions dying in summarisation.

    Extraction recovers ``>=2.1.8,<2.2.0`` correctly. Description summarisation
    then merges several raw descriptions into prose and the parseable field
    does not come out the other side.
    """

    def test_source_ordering_counts_as_survival(self):
        # The parser canonicalises to "<2.2.0,>=2.1.8"; a model reading the
        # file emits the file's ordering. Both are the same constraint.
        entities = entity_frame([("KOMBU", "library", "Version: >=2.1.8,<2.2.0")])
        findings, stats = verify.check_declared_constraints(
            RepoFacts(root="/r", packages=[CELERY_FACTS.packages[0]]), entities
        )
        self.assertEqual(findings, [])
        self.assertEqual(stats, {"declared_constraints": 1, "constraints_survived": 1,
                                 "constraints_unindexed": 0})

    def test_prose_paraphrase_is_reported_as_mangled(self):
        entities = entity_frame([
            ("KOMBU", "library", "A messaging library used with a version "
             "constraint of at least 2.1.8"),
        ])
        findings, stats = verify.check_declared_constraints(
            RepoFacts(root="/r", packages=[CELERY_FACTS.packages[0]]), entities
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, FindingKind.MANGLED)
        self.assertEqual(stats["constraints_survived"], 0)

    def test_dependency_with_no_entity_is_reported_as_missing(self):
        findings, stats = verify.check_declared_constraints(
            CELERY_FACTS, entity_frame([("KOMBU", "library", "Version: >=2.1.8,<2.2.0")])
        )
        missing = [f for f in findings if f.kind is FindingKind.MISSING]
        self.assertEqual(sorted(f.subject for f in missing), ["anyjson", "python-dateutil"])
        self.assertEqual(stats["declared_constraints"], 3)

    def test_counted_per_declaration_so_a_second_manifest_is_its_own_check(self):
        facts = RepoFacts(root="/r", packages=[
            package("python-dateutil", ">=1.5,<2.0", "python-dateutil>=1.5,<2.0",
                    "requirements/default.txt"),
            package("python-dateutil", ">=2.0", "python-dateutil>=2.0",
                    "requirements/default-py3k.txt"),
        ])
        entities = entity_frame([("PYTHON-DATEUTIL", "library", "Version: >=1.5,<2.0")])
        findings, stats = verify.check_declared_constraints(facts, entities)
        self.assertEqual(stats, {"declared_constraints": 2, "constraints_survived": 1,
                                 "constraints_unindexed": 0})
        self.assertEqual(findings[0].source_file, "requirements/default-py3k.txt")

    def test_a_manifest_that_was_never_indexed_is_held_out_of_the_score(self):
        # celery declares bundle in requirements/pkgutils.txt, which the run
        # never indexed. Counting it as a loss scores the model on a file no
        # model was shown, and it is what made the extraction rate read 3/16
        # when the honest denominator was 7.
        facts = RepoFacts(root="/r", packages=[
            package("kombu", ">=2.1.8", "kombu>=2.1.8", "requirements/default.txt"),
            package("bundle", ">=0.1", "bundle>=0.1", "requirements/pkgutils.txt"),
        ])
        corpus = Corpus(files=[
            CorpusFile(path="requirements/default.txt", git_repo="r", git_slug="s",
                       multi_repo=False),
        ])
        entities = entity_frame([("KOMBU", "library", "Version: >=2.1.8")])
        findings, stats = verify.check_declared_constraints(facts, entities, corpus)
        self.assertEqual(stats, {"declared_constraints": 1, "constraints_survived": 1,
                                 "constraints_unindexed": 1})
        self.assertEqual([f.kind for f in findings], [FindingKind.UNINDEXED])
        self.assertEqual(VerificationReport(findings=findings).defects, [])

    def test_name_spelling_differences_do_not_count_as_loss(self):
        facts = RepoFacts(root="/r", packages=[
            package("python-dateutil", ">=2.0", "python-dateutil>=2.0"),
        ])
        entities = entity_frame([("python_dateutil", "library", "Version: >=2.0")])
        findings, _ = verify.check_declared_constraints(facts, entities)
        self.assertEqual(findings, [])

    def test_a_version_in_the_title_still_names_the_distribution(self):
        # The short-prompt celery run emitted ``COVERAGE>=3.0`` as a title,
        # copying the requirements line whole. Keyed as written it looked like
        # a dependency missing from the graph, which scored that prompt for a
        # constraint it had in fact extracted.
        facts = RepoFacts(root="/r", packages=[
            package("coverage", ">=3.0", "coverage>=3.0"),
        ])
        entities = entity_frame([("COVERAGE>=3.0", "library", "Version: >=3.0")])
        findings, stats = verify.check_declared_constraints(facts, entities)
        self.assertEqual(findings, [])
        self.assertEqual(stats["constraints_survived"], 1)

    def test_a_title_that_is_a_phrase_cannot_satisfy_a_constraint(self):
        # The specifier strip has to be narrow. If it took the first word of
        # any title, prose mentioning a distribution would stand in for the
        # entity and the check would pass on nothing.
        facts = RepoFacts(root="/r", packages=[
            package("coverage", ">=3.0", "coverage>=3.0"),
        ])
        entities = entity_frame([("coverage of the test suite", "library", "Prose.")])
        findings, stats = verify.check_declared_constraints(facts, entities)
        self.assertEqual([f.kind for f in findings], [FindingKind.MISSING])
        self.assertEqual(stats["constraints_survived"], 0)


class TestPackageLayerGrounded(unittest.TestCase):
    """The inverse direction, which is the only way invention becomes visible."""

    ROWS = [
        ("kombu", "library", "A messaging library"),
        ("click", "library", "A command line toolkit"),
        ("celery.worker.utils.timeutils", "library", "Time helpers"),
        ("CELERY/WORKER/PLATFORMS", "library", "Platform helpers"),
        ("kombu.pools", "dependency", "Connection pooling"),
        ("os.path", "library", "Path helpers"),
        ("pyinotify", "library", "Filesystem notifications"),
        ("CELERY", "application", "The project"),
    ]

    def setUp(self):
        facts = RepoFacts(
            root=CELERY_FACTS.root,
            own_modules=CELERY_FACTS.own_modules,
            packages=CELERY_FACTS.packages,
            imported_names=["pyinotify", "os", "celery"],
        )
        self.findings, self.stats = verify.check_package_layer_grounded(
            facts, entity_frame(self.ROWS)
        )
        self.by_subject = {f.subject: f for f in self.findings}

    def test_a_name_nothing_declares_or_imports_is_invention(self):
        self.assertIn("no source file imports it", self.by_subject["click"].detail)
        self.assertEqual(self.stats["package_layer"]["invented"], 1)

    def test_an_imported_but_undeclared_dependency_is_not_an_invention(self):
        # celery 2.5.5 imports pyinotify inside a try/except and declares it
        # nowhere. Calling that a hallucination would hide a packaging gap.
        finding = self.by_subject["pyinotify"]
        self.assertEqual(finding.kind, FindingKind.UNDECLARED)
        self.assertEqual(self.stats["package_layer"]["undeclared"], 1)

    def test_the_repositorys_own_code_is_a_layer_error(self):
        self.assertIn("defined in this repository",
                      self.by_subject["celery.worker.utils.timeutils"].detail)

    def test_path_shaped_names_are_recognised_as_internal_too(self):
        # Models write the same module as celery.worker.x and celery/worker/x.
        self.assertIn("defined in this repository",
                      self.by_subject["CELERY/WORKER/PLATFORMS"].detail)
        self.assertEqual(self.stats["package_layer"]["internal"], 2)

    def test_a_symbol_inside_a_dependency_is_a_granularity_error(self):
        self.assertIn("not a distribution", self.by_subject["kombu.pools"].detail)

    def test_a_stdlib_module_typed_as_third_party_is_flagged(self):
        self.assertIn("ships with the language runtime", self.by_subject["os.path"].detail)

    def test_granularity_errors_are_counted_together(self):
        self.assertEqual(self.stats["package_layer"]["granularity"], 2)

    def test_a_declared_dependency_is_grounded(self):
        self.assertNotIn("kombu", self.by_subject)
        self.assertEqual(self.stats["package_layer"]["grounded"], 1)

    def test_entities_outside_the_package_types_are_not_examined(self):
        self.assertNotIn("CELERY", self.by_subject)

    def test_only_the_undeclared_case_is_not_a_defect(self):
        kinds = {f.kind for f in self.findings}
        self.assertEqual(kinds, {FindingKind.UNGROUNDED, FindingKind.UNDECLARED})


class TestStdlibLayer(unittest.TestCase):
    ROWS = [
        ("os", "stdlib_module", "Operating system interface"),
        ("urllib2", "stdlib_module", "Python 2 URL handling"),
        ("isinstance", "stdlib_module", "Type check"),
        ("celery.utils", "stdlib_module", "Project helpers"),
        ("kombu", "stdlib_module", "A messaging library"),
        ("billiard", "stdlib_module", "Process pooling"),
    ]

    def setUp(self):
        self.findings, self.stats = verify.check_stdlib_layer(
            CELERY_FACTS, entity_frame(self.ROWS)
        )
        self.by_subject = {f.subject: f for f in self.findings}
        self.counts = self.stats["stdlib_layer"]

    def test_a_real_stdlib_module_passes(self):
        self.assertNotIn("os", self.by_subject)

    def test_python2_only_modules_pass_because_the_corpus_is_old(self):
        # Checking against the running interpreter alone would flag urllib2 as
        # invented, which would be wrong for every repository this targets.
        self.assertNotIn("urllib2", self.by_subject)
        self.assertEqual(self.counts["ok"], 2)

    def test_a_builtin_is_a_category_error(self):
        self.assertIn("builtin", self.by_subject["isinstance"].detail)
        self.assertEqual(self.counts["not_a_module"], 1)

    def test_project_code_typed_as_stdlib_is_a_layer_error(self):
        self.assertIn("defined in this repository", self.by_subject["celery.utils"].detail)

    def test_a_third_party_distribution_typed_as_stdlib_is_unknown(self):
        self.assertIn("not in the standard library", self.by_subject["billiard"].detail)
        self.assertEqual(self.counts["unknown"], 1)

    def test_a_declared_distribution_shadows_the_runtime_module(self):
        # The case this check exists for: celery 2.5.5 pins
        # multiprocessing==2.6.2.1, a backport, so a name that is in
        # sys.stdlib_module_names is still a third-party dependency here.
        # kombu stands in for it because the fixture declares kombu.
        self.assertIn("shadows the runtime module", self.by_subject["kombu"].detail)
        self.assertEqual(self.counts["shadowed"], 1)


class TestModuleLayerGrounded(unittest.TestCase):
    """Five outcomes for a module entity, with five different causes.

    The corpus is a subset of the checkout, so "the corpus cannot confirm it"
    and "it does not exist" are different claims and the inventory is what
    separates them. On the restored celery run the bucket counted 40
    inventions, of which 19 were relative-import spellings the prompt had
    asked for.
    """

    #: Two files indexed out of a checkout that holds four.
    CORPUS = ["celery/worker/state.py", "celery/worker/heartbeat.py"]
    FACTS = RepoFacts(
        root="/tmp/celery-255",
        source_files=[
            "celery/worker/state.py",
            "celery/worker/heartbeat.py",
            "celery/utils/timer2.py",
            "celery/concurrency/processes.py",
        ],
    )

    #: (title, bucket). ``celery.utils`` and ``celery.concurrency`` are
    #: directories only the checkout proves; ``celery.worker`` is proved by an
    #: indexed path.
    CASES = [
        ("CELERY/WORKER/STATE.PY", "indexed"),
        ("CELERY.WORKER.STATE", "indexed"),
        ("CELERY.WORKER", "package"),
        ("CELERY.CONCURRENCY", "package_on_disk"),
        ("CELERY.UTILS.TIMER2", "leaked"),
        ("CELERY.WORKER.NONESUCH", "invented"),
    ]

    def corpus(self, paths=None):
        return Corpus(files=[
            CorpusFile(path=p, git_repo="https://github.com/celery/celery.git",
                       git_slug="celery-v2-5-5", multi_repo=False)
            for p in (paths or self.CORPUS)
        ])

    def counts(self, titles, facts=None, paths=None):
        entities = entity_frame([(t, "module", "") for t in titles])
        _findings, stats = verify.check_module_layer_grounded(
            facts or self.FACTS, entities, self.corpus(paths),
        )
        return stats["module_layer"]

    def test_each_title_lands_in_the_bucket_its_evidence_supports(self):
        for title, bucket in self.CASES:
            with self.subTest(title=title):
                self.assertEqual(self.counts([title])[bucket], 1, self.counts([title]))

    def test_every_module_entity_gets_a_verdict(self):
        counts = self.counts([t for t, _ in self.CASES])
        buckets = ("indexed", "package", "package_on_disk", "leaked", "invented")
        self.assertEqual(sum(counts[b] for b in buckets), len(self.CASES))

    def test_precision_ignores_the_checkout_proved_bucket(self):
        # The published figure means "grounded in what the model was shown",
        # and a directory only the checkout knows about was not shown. Adding
        # it to the numerator would move a number with a recorded history.
        counts = self.counts([t for t, _ in self.CASES])
        self.assertEqual(counts["precision"], round(3 / 6, 3))

    def test_without_the_inventory_leakage_cannot_be_told_from_invention(self):
        bare = RepoFacts(root="/tmp/celery-255")
        counts = self.counts(["CELERY.UTILS.TIMER2", "CELERY.CONCURRENCY"], bare)
        self.assertEqual(counts["leaked"], 0)
        self.assertEqual(counts["package_on_disk"], 0)
        self.assertEqual(counts["invented"], 2)

    def test_a_relative_import_is_resolved_before_it_is_judged(self):
        # ``.STATE`` from celery/worker/heartbeat.py is celery.worker.state,
        # which was indexed. Counting it as an invention charged the model for
        # obeying the prompt.
        counts = self.counts([".STATE"])
        self.assertEqual(counts["indexed"], 1)
        self.assertEqual(counts["relative_resolved"], 1)
        self.assertEqual(counts["relative_unresolved"], 0)

    def test_an_ambiguous_relative_import_is_left_unresolved(self):
        # Both celery/abstract.py and celery/app/abstract.py would satisfy
        # ``..ABSTRACT``, and without the text_units table nothing says which
        # document produced it.
        paths = [*self.CORPUS, "celery/app/base.py", "celery/app/task/base.py",
                 "celery/abstract.py", "celery/app/abstract.py"]
        counts = self.counts(["..ABSTRACT"], paths=paths)
        self.assertEqual(counts["relative_unresolved"], 1)
        self.assertEqual(counts["relative_resolved"], 0)

    def test_an_empty_corpus_reports_nothing_rather_than_everything(self):
        entities = entity_frame([("CELERY.WORKER.NONESUCH", "module", "")])
        findings, stats = verify.check_module_layer_grounded(
            self.FACTS, entities, Corpus(),
        )
        self.assertEqual((findings, stats), ([], {}))


class TestVerifyIndex(unittest.TestCase):
    def write_index(self, rows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        entity_frame(rows).to_parquet(path / "entities.parquet")
        return path

    def test_a_clean_index_produces_no_findings(self):
        facts = RepoFacts(
            root="/r",
            git_repo="https://github.com/celery/celery.git",
            git_slug="celery",
            own_modules=["celery"],
            packages=[package("kombu", "<2.2.0,>=2.1.8", "kombu>=2.1.8,<2.2.0")],
        )
        output = self.write_index([
            ("kombu", "library", "Messaging library. Version: >=2.1.8,<2.2.0"),
            ("CELERY", "application", "Repository hosted at "
             "https://github.com/celery/celery.git, slug celery"),
        ])
        report = verify.verify_index(facts, output)
        self.assertTrue(report.ok, msg=verify.render(report))
        self.assertEqual(report.checked, 2)

    def test_findings_are_grouped_by_kind_and_counted(self):
        output = self.write_index([
            ("kombu", "library", "A messaging library"),
            ("click", "library", "A command line toolkit"),
        ])
        report = verify.verify_index(CELERY_FACTS, output)
        counts = report.to_dict()["counts"]
        self.assertEqual(counts["missing"], 4)      # 2 git fields, 2 absent deps
        self.assertEqual(counts["mangled"], 1)      # kombu is present, its constraint is not
        self.assertEqual(counts["ungrounded"], 1)   # click
        self.assertFalse(report.ok)

    def test_render_reports_the_survival_ratio(self):
        output = self.write_index([("kombu", "library", "A messaging library")])
        text = verify.render(verify.verify_index(CELERY_FACTS, output))
        self.assertIn("declared constraints surviving into the graph: 0/3", text)

    def test_a_missing_entity_table_names_the_path_it_looked_for(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.assertRaises(FileNotFoundError) as caught:
            verify.verify_index(CELERY_FACTS, Path(directory.name))
        self.assertIn("entities.parquet", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
