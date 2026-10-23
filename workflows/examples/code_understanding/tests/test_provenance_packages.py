"""Writing the package layer instead of hoping the extraction emits it.

The cases here are the four rules :mod:`provenance.packages` applies, and
each one is pinned because a corpus in reach breaks it. Celery supplies the
first three: ``celery/db`` has no indexed ``__init__.py`` and so is a
directory nothing names, ``celery/app/task/__init__.py`` declares
``celery.app.task`` and so is a document rather than a directory, and the
120b run emitted ``CELERY.UTILS`` itself. The Maven layout supplies the
fourth, where the path fold proves ``src.main.java`` and no migration report
wants that node.

One claim here is a measurement rather than a rule, and it is the reason the
module exists at all: on the two graded celery arms this pass writes one
entity and zero. Python packages are ``__init__.py`` files, so most proved
directories are already documents. The languages that have no such file are
where the layer is genuinely absent, which is what the Maven case stands in
for until a Java index exists to measure.
"""

import unittest

from provenance import graph, packages
from provenance.corpus import Corpus, CorpusFile
from provenance.grounding_report import GroundingReport, render

REPO = "https://github.com/celery/celery.git"
SLUG = "celery-v2-5-5"


def corpus(paths, packages_=None):
    """A corpus from paths, with the ``Package:`` line each header states."""
    packages_ = packages_ or {}
    return Corpus(files=[
        CorpusFile(path=p, git_repo=REPO, git_slug=SLUG, multi_repo=False,
                   package=packages_.get(p))
        for p in paths
    ])


def entity_frame(rows):
    """``(title, type, description)`` triples, as GraphRAG stores them."""
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "type", "description"])


def synthesize(corp, rows):
    report = GroundingReport()
    return packages.synthesize(corp, entity_frame(rows), report), report


def titles(frame):
    return set(frame["title"].astype(str))


class TestWhatGetsWritten(unittest.TestCase):
    """A directory an indexed path proves, and no entity names."""

    PATHS = ["celery/db/models.py", "celery/db/session.py"]

    def test_a_proved_directory_with_no_entity_is_written_once(self):
        frame, report = self.synth([("CELERY/DB/MODELS.PY", "MODULE", "Models.")])
        self.assertEqual(report.packages_created, ["CELERY", "CELERY.DB"])
        self.assertEqual(sorted(titles(frame) - {"CELERY/DB/MODELS.PY"}),
                         ["CELERY", "CELERY.DB"])
        self.assertEqual(len(frame[frame["title"] == "CELERY.DB"]), 1)

    def test_the_row_is_shaped_like_the_ones_ground_modules_writes(self):
        frame, _ = self.synth([])
        row = frame[frame["title"] == "CELERY.DB"].iloc[0]
        self.assertEqual(row["type"], "PACKAGE")
        self.assertEqual(row["frequency"], 1)
        self.assertEqual(list(row["text_unit_ids"]), [])

    def test_the_description_states_the_repository_and_the_member_count(self):
        frame, _ = self.synth([])
        description = frame[frame["title"] == "CELERY.DB"].iloc[0]["description"]
        self.assertIn(REPO, description)
        self.assertIn("2 indexed source files", description)

    def test_a_single_member_is_not_pluralised(self):
        frame, _ = synthesize(corpus(["celery/db/models.py"]), [])
        description = frame[frame["title"] == "CELERY.DB"].iloc[0]["description"]
        self.assertIn("1 indexed source file.", description)

    def test_running_it_twice_writes_nothing_the_second_time(self):
        once, _ = self.synth([])
        again = packages.synthesize(self.corpus, once, GroundingReport())
        self.assertEqual(titles(again), titles(once))
        self.assertEqual(len(again), len(once))

    def test_the_report_renders_the_count(self):
        _, report = self.synth([])
        self.assertIn("2 directory(ies) proved by an indexed path", render(report))

    def setUp(self):
        self.corpus = corpus(self.PATHS)

    def synth(self, rows):
        return synthesize(self.corpus, rows)


class TestWhatIsLeftAlone(unittest.TestCase):
    """Three populations a second row would damage rather than complete."""

    def test_a_directory_the_extraction_named_is_not_duplicated(self):
        frame, report = synthesize(
            corpus(["celery/utils/timer2.py"]),
            [("CELERY.UTILS", "PACKAGE", "Utility helpers.")],
        )
        self.assertNotIn("CELERY.UTILS", report.packages_created)
        self.assertEqual(len(frame[frame["title"] == "CELERY.UTILS"]), 1)
        self.assertEqual(frame.iloc[0]["description"], "Utility helpers.")

    def test_the_title_is_matched_whatever_type_carries_it(self):
        """``finalize_entities`` collapses duplicate titles by position.

        A second ``CELERY.UTILS`` under a different type would leave which
        description survives to iteration order, so the check cannot be
        narrowed to the module-shaped rows.
        """
        frame, report = synthesize(
            corpus(["celery/utils/timer2.py"]),
            [("CELERY.UTILS", "CLASS", "Misfiled by the extraction, and still a row.")],
        )
        self.assertNotIn("CELERY.UTILS", report.packages_created)
        self.assertEqual(len(frame[frame["title"] == "CELERY.UTILS"]), 1)

    def test_a_prefix_that_is_itself_a_document_is_skipped(self):
        """``celery/app/task/__init__.py`` declares ``celery.app.task``.

        Which makes it an indexed document. This is the ordinary Python case
        rather than an edge one: a package *is* its ``__init__.py``, and the
        measured effect on the celery parser arm is that thirteen of eighteen
        proved directories are skipped here.
        """
        corp = corpus(
            ["celery/app/task/__init__.py", "celery/app/task/sets.py"],
            {"celery/app/task/__init__.py": "celery.app.task"},
        )
        self.assertIn("celery.app.task", corp.keys)
        _, report = synthesize(corp, [])
        self.assertEqual(report.packages_created, ["CELERY", "CELERY.APP"])

    def test_an_empty_corpus_returns_the_frame_untouched(self):
        rows = [("CELERY.UTILS", "PACKAGE", "Utility helpers.")]
        frame = entity_frame(rows)
        self.assertIs(packages.synthesize(Corpus(), frame, GroundingReport()), frame)


class TestOnlyNamesALanguageWouldImport(unittest.TestCase):
    """Rule 3 and rule 4, which live in the corpus rather than the entity table."""

    MAVEN = {
        "src/main/java/org/apache/commons/lang3/StringUtils.java":
            "org.apache.commons.lang3.StringUtils",
        "src/main/java/org/apache/commons/lang3/time/DateUtils.java":
            "org.apache.commons.lang3.time.DateUtils",
    }

    def test_the_build_layout_is_not_written_as_packages(self):
        corp = corpus(list(self.MAVEN), self.MAVEN)
        self.assertLessEqual({"src", "src.main", "src.main.java"},
                             corp.package_prefixes())
        _, report = synthesize(corp, [])
        self.assertEqual(report.packages_created, [
            "ORG", "ORG.APACHE", "ORG.APACHE.COMMONS", "ORG.APACHE.COMMONS.LANG3",
            "ORG.APACHE.COMMONS.LANG3.TIME",
        ])

    def test_a_declared_name_two_files_claim_falls_back_to_the_path(self):
        """Maven's parallel main and test trees, which ``keyed_files`` drops.

        The declared key is gone, so the path fold is all that is left and
        ``src`` comes back. Pinned rather than fixed: inferring a package name
        from a path the language does not use to find it is the guess this
        whole pass refuses to make, and a corpus holding only contested names
        has no uncontested evidence to offer.
        """
        contested = {
            "src/main/java/org/ex/Cache.java": "org.ex.Cache",
            "src/test/java/org/ex/Cache.java": "org.ex.Cache",
        }
        _, report = synthesize(corpus(list(contested), contested), [])
        self.assertIn("SRC", report.packages_created)

    def test_a_path_the_folding_cannot_strip_contributes_nothing(self):
        """``.kt`` is not in the folded suffix set, so every prefix is garbage.

        Same gate as ``graph._drop_unfoldable``, and for the same reason: a
        key that could never have matched an entity carries no information
        about what the directory above it is called.
        """
        corp = corpus(["src/main/kotlin/com/example/Foo.kt"])
        self.assertIn("src.main.kotlin.com.example.foo.kt", corp.keys)
        self.assertIn("src.main.kotlin.com.example", corp.package_prefixes())
        self.assertEqual(corp.importable_prefixes(), set())
        _, report = synthesize(corp, [])
        self.assertEqual(report.packages_created, [])

    def test_grading_stays_generous_while_writing_stays_narrow(self):
        """``package_prefixes`` is the wider set, and deliberately so.

        It is what ``verify`` charges a model against, where naming a real
        directory should not count as invention. Nothing here may narrow it.
        """
        corp = corpus(list(self.MAVEN), self.MAVEN)
        self.assertLess(corp.importable_prefixes(), corp.package_prefixes())


class TestThroughGround(unittest.TestCase):
    """The placement inside ``ground``, which is what keeps the node connected."""

    def frames(self):
        import pandas as pd

        entities = pd.DataFrame(
            [("CELERY/DB/MODELS.PY", "MODULE", "Models.")],
            columns=["title", "type", "description"],
        )
        edges = pd.DataFrame([], columns=["source", "target", "description", "weight"])
        return entities, edges

    def test_a_synthesized_package_gets_its_repository_edge(self):
        """``ground_repository`` runs after this pass, so the edge is free.

        Written as a test because the ordering is the only thing holding it
        up: a row appended after ``_belongs_to`` would land isolated, and an
        isolated node is invisible to every downstream query.
        """
        entities, edges = self.frames()
        corp = corpus(["celery/db/models.py"])
        entities, relationships, report = graph.ground(corp, entities, edges)

        self.assertIn("CELERY.DB", report.packages_created)
        self.assertIn("CELERY.DB", set(entities["title"].astype(str)))
        edge_ends = set(relationships["source"]) | set(relationships["target"])
        self.assertIn("CELERY.DB", edge_ends)

    def test_the_pass_adds_no_module_shaped_row_for_a_file(self):
        """``created`` and ``packages_created`` answer different questions.

        One is files the extraction missed, the other is directories it was
        never going to name. A reader comparing a joined arm against an
        unjoined one needs the split.
        """
        entities, edges = self.frames()
        corp = corpus(["celery/db/models.py", "celery/db/session.py"])
        _, _, report = graph.ground(corp, entities, edges)

        self.assertIn("CELERY/DB/SESSION.PY", report.created)
        self.assertEqual(report.packages_created, ["CELERY", "CELERY.DB"])
        self.assertFalse(set(report.created) & set(report.packages_created))


if __name__ == "__main__":
    unittest.main()
