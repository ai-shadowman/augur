"""Folding the graph back onto the files that were actually indexed.

Every case here is taken from the celery 2.5.5 run: 22 files indexed, 100
module-shaped entities returned, 26 of them naming a file that exists. The
rest were duplicate spellings of real files or paths assembled from two
directories the model had seen in different chunks. None of that needs a
model to resolve, because AUGUR wrote the paths into the headers itself.
"""

import unittest
from dataclasses import replace

from provenance import graph
from provenance.corpus import Corpus, CorpusFile
from provenance.grounding_report import GroundingReport, render
from provenance.models import RepoFacts

REPO = "https://github.com/celery/celery.git"
SLUG = "celery-v2-5-5"


def corpus(paths, runtime=None, slug=SLUG):
    return Corpus(files=[
        CorpusFile(path=p, git_repo=REPO, git_slug=slug, multi_repo=False, runtime=runtime)
        for p in paths
    ])


def entity_frame(rows):
    """``(title, type, description)`` triples, as GraphRAG stores them."""
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "type", "description"])


def edge_frame(rows=()):
    import pandas as pd

    return pd.DataFrame(list(rows), columns=["source", "target", "description", "weight"])


class TestGroundModules(unittest.TestCase):
    """The duplicate-spelling problem, which is three quarters of the defect."""

    def setUp(self):
        self.corpus = corpus(["celery/worker/state.py", "celery/app/base.py"])

    def ground(self, rows, edges=()):
        report = GroundingReport()
        entities, relationships = graph.ground_modules(
            self.corpus, entity_frame(rows), edge_frame(edges), report
        )
        return entities, relationships, report

    def test_three_spellings_of_one_file_become_one_entity(self):
        entities, _, report = self.ground([
            ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state tracking."),
            ("CELERY.WORKER.STATE", "MODULE", "State."),
            ("CELERY/WORKER/STATE", "MODULE", "The longest description of the three."),
            ("CELERY/APP/BASE.PY", "MODULE", "Application base."),
        ])
        titles = sorted(entities["title"])
        self.assertEqual(titles, ["CELERY/APP/BASE.PY", "CELERY/WORKER/STATE.PY"])
        self.assertEqual(report.merged,
                         [("CELERY/WORKER/STATE.PY", ["CELERY.WORKER.STATE", "CELERY/WORKER/STATE"])])

    def test_the_surviving_row_takes_the_header_spelling(self):
        entities, _, _ = self.ground([
            ("CELERY.WORKER.STATE", "MODULE", "State."),
            ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state tracking."),
        ])
        self.assertIn("CELERY/WORKER/STATE.PY", set(entities["title"]))

    def test_the_longest_description_survives_the_merge(self):
        # The rule is a judgement either way, so it is at least stated: the
        # duplicate spellings are usually a full description beside a stub.
        entities, _, _ = self.ground([
            ("CELERY/WORKER/STATE.PY", "MODULE", "State."),
            ("CELERY.WORKER.STATE", "MODULE", "Tracks which tasks the worker is running."),
        ])
        description = entities.set_index("title").at["CELERY/WORKER/STATE.PY", "description"]
        self.assertEqual(description, "Tracks which tasks the worker is running.")

    def test_edges_follow_the_titles_that_survived(self):
        _, relationships, report = self.ground(
            [("CELERY.WORKER.STATE", "MODULE", "State."),
             ("CELERY/APP/BASE.PY", "MODULE", "Base.")],
            [("CELERY/APP/BASE.PY", "CELERY.WORKER.STATE", "imports", 8.0)],
        )
        self.assertEqual(list(relationships["target"]), ["CELERY/WORKER/STATE.PY"])
        self.assertEqual(report.edges_rewritten, 1)

    def test_a_merge_that_creates_a_self_loop_drops_it(self):
        # Two spellings of one file with an edge between them is the model
        # relating a file to itself without noticing.
        _, relationships, report = self.ground(
            [("CELERY.WORKER.STATE", "MODULE", "State."),
             ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state.")],
            [("CELERY.WORKER.STATE", "CELERY/WORKER/STATE.PY", "uses", 5.0)],
        )
        self.assertEqual(len(relationships), 0)
        self.assertEqual(report.self_loops_removed, 1)

    def test_an_indexed_file_with_no_entity_is_created(self):
        # A recall failure is the one thing a join can repair outright: the
        # path is known and the type is not in doubt.
        entities, _, report = self.ground([
            ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state."),
        ])
        self.assertEqual(report.created, ["CELERY/APP/BASE.PY"])
        created = entities.set_index("title").loc["CELERY/APP/BASE.PY"]
        self.assertEqual(created["type"], "MODULE")
        self.assertIn("celery/app/base.py", created["description"])

    def test_a_directory_an_indexed_path_proves_is_kept_as_a_package(self):
        entities, _, _ = self.ground([
            ("CELERY/WORKER", "MODULE", "The worker subpackage."),
            ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state."),
            ("CELERY/APP/BASE.PY", "MODULE", "Base."),
        ])
        row = entities.set_index("title").loc["CELERY.WORKER"]
        self.assertEqual(row["type"], "PACKAGE")

    def test_a_module_naming_no_indexed_file_is_reported_not_deleted(self):
        # The corpus is a subset of the repository, so "not indexed" is not
        # "does not exist". These carry relationships; verify splits them
        # against the checkout into leakage and invention.
        entities, _, report = self.ground([
            ("CELERY/WORKER/APP.PY", "MODULE", "A path assembled from two chunks."),
            ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state."),
            ("CELERY/APP/BASE.PY", "MODULE", "Base."),
        ])
        self.assertEqual(report.ungrounded_modules, ["CELERY/WORKER/APP.PY"])
        self.assertIn("CELERY/WORKER/APP.PY", set(entities["title"]))

    def test_entities_that_are_not_module_shaped_are_left_alone(self):
        entities, _, _ = self.ground([
            ("KOMBU", "DEPENDENCY", "A messaging library."),
            ("CELERY/WORKER/STATE.PY", "MODULE", "Worker state."),
            ("CELERY/APP/BASE.PY", "MODULE", "Base."),
        ])
        self.assertEqual(entities.set_index("title").at["KOMBU", "type"], "DEPENDENCY")

    def test_an_empty_corpus_leaves_the_tables_untouched(self):
        report = GroundingReport()
        rows = [("CELERY.WORKER.STATE", "MODULE", "State.")]
        entities, _ = graph.ground_modules(Corpus(), entity_frame(rows), edge_frame(), report)
        self.assertEqual(list(entities["title"]), ["CELERY.WORKER.STATE"])
        self.assertEqual(report.merged, [])


class TestRelativeImports(unittest.TestCase):
    """The dots the prompt asks the model to keep, resolved after extraction.

    ``extract_graph.txt`` tells the model to leave a relative import's leading
    dots alone because resolving them needs the importing file's own path, and
    promises the calculation happens afterwards. These are that calculation.
    On the restored celery run, 19 of the 40 module entities counted as
    inventions were spellings of this kind.
    """

    PATHS = [
        "celery/worker/heartbeat.py",
        "celery/worker/state.py",
        "celery/app/base.py",
        "celery/app/task/base.py",
    ]

    def ground(self, rows, text_units=None, paths=None, facts=None):
        report = GroundingReport()
        entities, _ = graph.ground_modules(
            corpus(paths or self.PATHS), entity_frame(rows), edge_frame(), report,
            text_units, facts,
        )
        return entities, report

    def unit_frame(self, rows):
        import pandas as pd

        return pd.DataFrame(rows, columns=["id", "document_ids"])

    def test_a_unique_resolution_lands_on_the_indexed_file(self):
        # ``.STATE`` resolves to celery.worker.state from the only document
        # that can produce it, and the existing merge then gives it the header
        # spelling.
        entities, report = self.ground([(".STATE", "MODULE", "Worker state.")])
        self.assertIn("CELERY/WORKER/STATE.PY", set(entities["title"]))
        self.assertEqual(report.relative_resolved, [(".STATE", "CELERY.WORKER.STATE")])
        self.assertEqual(report.relative_unresolved, [])

    def test_an_ambiguous_name_is_left_alone_without_its_document(self):
        # ``..ABSTRACT`` means celery.abstract from celery/app/base.py and
        # celery.app.abstract from celery/app/task/base.py. Both are plausible
        # and only the producing document decides, so nothing is guessed.
        paths = [*self.PATHS, "celery/abstract.py", "celery/app/abstract.py"]
        entities, report = self.ground([("..ABSTRACT", "MODULE", "Abstract.")], paths=paths)
        self.assertIn("..ABSTRACT", set(entities["title"]))
        self.assertEqual(report.relative_unresolved, ["..ABSTRACT"])

    def test_the_producing_document_settles_the_ambiguity(self):
        paths = [*self.PATHS, "celery/abstract.py", "celery/app/abstract.py"]
        rows = [("..ABSTRACT", "MODULE", "Abstract.")]
        frame = entity_frame(rows)
        frame["text_unit_ids"] = [["u1"]]
        # Give the corpus the document ids GraphRAG would have written.
        indexed = Corpus(files=[
            replace(f, document_id=f"d{i}") for i, f in enumerate(corpus(paths).files)
        ])
        producing = next(f for f in indexed.files if f.path == "celery/app/task/base.py")
        units = self.unit_frame([("u1", [producing.document_id])])

        report = GroundingReport()
        entities, _ = graph.ground_modules(indexed, frame, edge_frame(), report, units)
        self.assertEqual(
            report.relative_resolved, [("..ABSTRACT", "CELERY.APP.ABSTRACT")],
        )
        self.assertIn("CELERY/APP/ABSTRACT.PY", set(entities["title"]))

    def test_a_symbol_shaped_name_is_not_forced_onto_a_path(self):
        # ``..UTILS.CACHED_PROPERTY`` resolves to celery.utils.cached_property,
        # which no indexed path backs, so it stays as the model wrote it.
        entities, report = self.ground(
            [("..UTILS.CACHED_PROPERTY", "MODULE", "A descriptor.")]
        )
        self.assertIn("..UTILS.CACHED_PROPERTY", set(entities["title"]))
        self.assertEqual(report.relative_unresolved, ["..UTILS.CACHED_PROPERTY"])

    def test_edges_follow_the_resolved_title(self):
        report = GroundingReport()
        edges = [(".STATE", "CELERY/APP/BASE.PY", "imports", 1.0)]
        _entities, relationships = graph.ground_modules(
            corpus(self.PATHS),
            entity_frame([(".STATE", "MODULE", "Worker state.")]),
            edge_frame(edges), report,
        )
        self.assertEqual(list(relationships["source"]), ["CELERY/WORKER/STATE.PY"])

    def test_the_checkout_can_contradict_a_corpus_only_answer(self):
        # The run that found this: ``..ABSTRACT`` has two candidates and only
        # ``celery/app/abstract.py`` was indexed, so corpus-only evidence made
        # the wrong one look unique. Every celery file that writes
        # ``from ..abstract import`` sits in celery/worker/ and means
        # ``celery.abstract``. The checkout knows that file is there.
        rows = [("..ABSTRACT", "MODULE", "Abstract.")]
        paths = [*self.PATHS, "celery/app/abstract.py"]

        _entities, corpus_only = self.ground(rows, paths=paths)
        self.assertEqual(
            corpus_only.relative_resolved, [("..ABSTRACT", "CELERY.APP.ABSTRACT")],
        )

        facts = RepoFacts(root="/tmp/celery", source_files=[
            *paths, "celery/abstract.py", "celery/worker/autoscale.py",
        ])
        entities, with_checkout = self.ground(rows, paths=paths, facts=facts)
        self.assertEqual(with_checkout.relative_resolved, [])
        self.assertEqual(with_checkout.relative_unresolved, ["..ABSTRACT"])
        self.assertIn("..ABSTRACT", set(entities["title"]))

    def test_the_checkout_settles_a_name_the_corpus_cannot(self):
        # ``..UTILS.TIMER2`` resolves to celery.utils.timer2 from any of the
        # indexed worker files. No indexed path backs it, so corpus-only
        # evidence refuses; the checkout has the file and the dots come off.
        rows = [("..UTILS.TIMER2", "MODULE", "A timer.")]

        _entities, corpus_only = self.ground(rows)
        self.assertEqual(corpus_only.relative_unresolved, ["..UTILS.TIMER2"])

        facts = RepoFacts(
            root="/tmp/celery", source_files=[*self.PATHS, "celery/utils/timer2.py"],
        )
        entities, with_checkout = self.ground(rows, facts=facts)
        self.assertEqual(
            with_checkout.relative_resolved, [("..UTILS.TIMER2", "CELERY.UTILS.TIMER2")],
        )
        # Resolved, not grounded: the name is now spelled correctly and the
        # module layer is free to report it as naming an unindexed file.
        self.assertIn("CELERY.UTILS.TIMER2", set(entities["title"]))


HEADER = (
    '"""This file is located at {path} from repository url '
    "https://github.com/celery/celery, repository slug celery-v2-5-5, "
    'multi_repo false, runtime Python 2.7"""\n'
)


def document_frame(rows):
    """A minimal GraphRAG ``documents`` table: a header plus a body.

    pandas is imported here rather than at module scope because
    ``conftest.real_dataframes`` only swaps the stub out once the module is
    already imported.
    """
    import pandas as pd

    return pd.DataFrame([
        {"title": path, "text": HEADER.format(path=path) + body} for path, body in rows
    ])


class TestGroundSymbols(unittest.TestCase):
    """Class and function entities filed under a module that does not declare them.

    On the restored celery run, 102 of 421 symbol entities named a real symbol
    and the wrong owner. The corpus names the right owner for most of them, so
    this is a join. What it must not do is move a symbol the corpus cannot
    vouch for, because the leaked-versus-invented split downstream reads the
    title the model wrote.
    """

    DOCUMENTS = [
        ("celery/worker/state.py", "def task_accepted(): pass\n\nclass LocalCache:\n"
                                   "    def get(self): pass\n"),
        ("celery/app/base.py", "class App:\n    def config_from_object(self): pass\n"),
    ]

    def ground(self, rows, edges=(), documents=None, facts=None):
        report = GroundingReport()
        entities, relationships = graph.ground_symbols(
            entity_frame(rows), edge_frame(edges), report,
            document_frame(documents or self.DOCUMENTS), facts,
        )
        return entities, relationships, report

    def test_a_misattributed_function_moves_to_the_module_that_declares_it(self):
        entities, _, report = self.ground(
            [("CELERY.APP.BASE.TASK_ACCEPTED", "FUNCTION", "Marks a task accepted.")]
        )
        self.assertEqual(list(entities["title"]), ["CELERY.WORKER.STATE.TASK_ACCEPTED"])
        self.assertEqual(list(entities["type"]), ["FUNCTION"])
        self.assertEqual(report.symbols_refiled, [
            ("CELERY.APP.BASE.TASK_ACCEPTED", "CELERY.WORKER.STATE.TASK_ACCEPTED"),
        ])

    def test_a_qualified_method_keeps_its_class(self):
        entities, _, report = self.ground(
            [("CELERY.APP.BASE.LOCALCACHE.GET", "FUNCTION", "Reads the cache.")]
        )
        self.assertEqual(list(entities["title"]), ["CELERY.WORKER.STATE.LOCALCACHE.GET"])
        self.assertEqual(len(report.symbols_refiled), 1)

    def test_a_correctly_filed_symbol_is_left_alone(self):
        entities, _, report = self.ground(
            [("CELERY.WORKER.STATE.TASK_ACCEPTED", "FUNCTION", "Correct already.")]
        )
        self.assertEqual(list(entities["title"]), ["CELERY.WORKER.STATE.TASK_ACCEPTED"])
        self.assertEqual(report.symbols_refiled, [])

    def test_a_name_the_corpus_cannot_vouch_for_is_left_alone(self):
        # Invention or leakage, and verify tells those apart by reading the
        # title. Renaming it here would destroy the evidence.
        entities, _, report = self.ground(
            [("CELERY.UTILS.NONESUCH", "FUNCTION", "Nothing declares this.")]
        )
        self.assertEqual(list(entities["title"]), ["CELERY.UTILS.NONESUCH"])
        self.assertEqual(report.symbols_refiled, [])

    def test_two_declaring_modules_make_the_answer_ambiguous(self):
        documents = [
            ("celery/worker/state.py", "def reset(): pass\n"),
            ("celery/app/base.py", "def reset(): pass\n"),
        ]
        entities, _, report = self.ground(
            [("CELERY.UTILS.RESET", "FUNCTION", "Resets something.")],
            documents=documents,
        )
        self.assertEqual(list(entities["title"]), ["CELERY.UTILS.RESET"])
        self.assertEqual(report.symbols_refiled, [])
        self.assertEqual(report.symbols_ambiguous, ["CELERY.UTILS.RESET"])

    def test_the_checkout_protects_a_symbol_from_an_indexed_namesake(self):
        # celery/utils/timer2.py declares ``apply_after`` and was never
        # indexed; an indexed module declares a function of the same name.
        # Without the checkout this is a rename onto the namesake, which is
        # the ``..ABSTRACT`` mistake one layer down.
        documents = [("celery/worker/state.py", "def apply_after(): pass\n")]
        rows = [("CELERY.UTILS.TIMER2.APPLY_AFTER", "FUNCTION", "Schedules work.")]

        _entities, _edges, without = self.ground(rows, documents=documents)
        self.assertEqual(without.symbols_refiled, [
            ("CELERY.UTILS.TIMER2.APPLY_AFTER", "CELERY.WORKER.STATE.APPLY_AFTER"),
        ])

        facts = RepoFacts(
            root="/tmp/celery",
            declared_symbols={"celery.utils.timer2": ["apply_after"]},
        )
        entities, _edges, with_checkout = self.ground(
            rows, documents=documents, facts=facts,
        )
        self.assertEqual(with_checkout.symbols_refiled, [])
        self.assertEqual(list(entities["title"]), ["CELERY.UTILS.TIMER2.APPLY_AFTER"])

    def test_a_correction_folds_into_the_row_that_was_already_right(self):
        rows = [
            ("CELERY.WORKER.STATE.TASK_ACCEPTED", "FUNCTION", "The full description."),
            ("CELERY.APP.BASE.TASK_ACCEPTED", "FUNCTION", "Stub."),
        ]
        entities, _, report = self.ground(rows)
        self.assertEqual(list(entities["title"]), ["CELERY.WORKER.STATE.TASK_ACCEPTED"])
        self.assertEqual(list(entities["description"]), ["The full description."])
        self.assertEqual(len(report.symbols_refiled), 1)

    def test_edges_follow_the_refiled_symbol(self):
        edges = [("CELERY.APP.BASE.TASK_ACCEPTED", "CELERY/APP/BASE.PY", "declared in", 1.0)]
        _entities, relationships, _report = self.ground(
            [("CELERY.APP.BASE.TASK_ACCEPTED", "FUNCTION", "Marks a task accepted.")],
            edges=edges,
        )
        self.assertEqual(
            list(relationships["source"]), ["CELERY.WORKER.STATE.TASK_ACCEPTED"]
        )

    def test_a_module_entity_is_not_a_symbol_and_is_not_touched(self):
        entities, _, report = self.ground(
            [("CELERY.APP.BASE.TASK_ACCEPTED", "MODULE", "Shaped like a symbol.")]
        )
        self.assertEqual(list(entities["title"]), ["CELERY.APP.BASE.TASK_ACCEPTED"])
        self.assertEqual(report.symbols_refiled, [])

    def test_without_indexed_declarations_nothing_moves(self):
        entities, _, report = self.ground(
            [("CELERY.APP.BASE.TASK_ACCEPTED", "FUNCTION", "Marks a task accepted.")],
            documents=[("requirements/default.txt", "kombu = 2.1.8\n")],
        )
        self.assertEqual(list(entities["title"]), ["CELERY.APP.BASE.TASK_ACCEPTED"])
        self.assertEqual(report.symbols_refiled, [])


class TestGroundRepository(unittest.TestCase):
    """One repository was indexed, so the graph gets exactly one repository."""

    def ground(self, rows, edges=(), runtime=None, slug=SLUG):
        report = GroundingReport()
        entities, relationships = graph.ground_repository(
            corpus(["celery/app/base.py"], runtime=runtime, slug=slug),
            entity_frame(rows), edge_frame(edges), report,
        )
        return entities, relationships, report

    def test_the_slug_from_the_header_names_the_entity(self):
        entities, _, report = self.ground([("CELERY-V2-5-5", "REPOSITORY", "The task queue.")])
        self.assertEqual(report.repository, "CELERY-V2-5-5")
        self.assertIn("CELERY-V2-5-5", set(entities["title"]))

    def test_the_duplicates_a_lost_header_produces_are_absorbed(self):
        # celery produced three: the correct one plus UNKNOWN and
        # UNKNOWN_REPO, from chunks where the model lost the header.
        entities, _, report = self.ground([
            ("UNKNOWN", "REPOSITORY", "A repository."),
            ("CELERY-V2-5-5", "REPOSITORY", "The task queue."),
            ("UNKNOWN_REPO", "REPOSITORY", "A repository."),
        ])
        self.assertEqual(report.repository_absorbed, ["UNKNOWN", "UNKNOWN_REPO"])
        self.assertEqual(list(entities["type"]).count("REPOSITORY"), 1)

    def test_edges_into_the_absorbed_duplicates_are_redirected(self):
        _, relationships, _ = self.ground(
            [("UNKNOWN_REPO", "REPOSITORY", "A repository."),
             ("CELERY/APP/BASE.PY", "MODULE", "Base.")],
            [("CELERY/APP/BASE.PY", "UNKNOWN_REPO", "belongs to", 10.0)],
        )
        self.assertEqual(list(relationships["target"]), ["CELERY-V2-5-5"])

    def test_the_url_and_runtime_are_written_in_from_the_header(self):
        entities, _, _ = self.ground(
            [("CELERY-V2-5-5", "REPOSITORY", "The distributed task queue.")],
            runtime="Python 2.7",
        )
        description = entities.set_index("title").at["CELERY-V2-5-5", "description"]
        self.assertEqual(description, f"Repository hosted at {REPO}. Runtime stack: "
                                      "Python 2.7. The distributed task queue.")

    def test_grounding_twice_does_not_repeat_the_identity_sentences(self):
        entities, relationships, _ = self.ground(
            [("CELERY-V2-5-5", "REPOSITORY", "The task queue.")], runtime="Python 2.7")
        report = GroundingReport()
        again, _ = graph.ground_repository(
            corpus(["celery/app/base.py"], runtime="Python 2.7"),
            entities, relationships, report,
        )
        description = again.set_index("title").at["CELERY-V2-5-5", "description"]
        self.assertEqual(description.count("Repository hosted at"), 1)
        self.assertEqual(description.count("Runtime stack"), 1)

    def test_a_repository_the_model_never_emitted_is_created(self):
        entities, _, report = self.ground([("CELERY/APP/BASE.PY", "MODULE", "Base.")])
        self.assertIn("CELERY-V2-5-5", report.created)
        self.assertEqual(
            entities.set_index("title").at["CELERY-V2-5-5", "description"],
            f"Repository hosted at {REPO}.",
        )

    def test_headers_with_no_slug_leave_the_entity_alone_and_say_so(self):
        with self.assertLogs("provenance.graph", level="WARNING"):
            entities, _, report = self.ground(
                [("CELERY", "REPOSITORY", "The task queue.")], slug="")
        self.assertIsNone(report.repository)
        self.assertEqual(list(entities["title"]), ["CELERY"])


class TestBelongsTo(unittest.TestCase):
    """The edge the prompt used to ask for, emitted from the join instead."""

    def ground(self, rows, edges=()):
        report = GroundingReport()
        entities, relationships = graph.ground_repository(
            corpus(["celery/app/base.py"]), entity_frame(rows), edge_frame(edges), report,
        )
        return entities, relationships, report

    def test_every_module_gains_an_edge_to_the_repository(self):
        _, relationships, report = self.ground([
            ("CELERY-V2-5-5", "REPOSITORY", "The task queue."),
            ("CELERY/APP/BASE.PY", "MODULE", "Base."),
            ("CELERY.APP", "PACKAGE", "The app subpackage."),
        ])
        targets = relationships[relationships["target"] == "CELERY-V2-5-5"]
        self.assertEqual(sorted(targets["source"]), ["CELERY.APP", "CELERY/APP/BASE.PY"])
        self.assertEqual(report.belongs_to_added, 2)

    def test_classes_functions_and_dependencies_get_no_edge(self):
        # A class is reachable through its module, and an external library
        # belongs to nobody here.
        _, relationships, report = self.ground([
            ("CELERY-V2-5-5", "REPOSITORY", "The task queue."),
            ("CELERY.APP.BASE.APP", "CLASS", "The app class."),
            ("CELERY.APP.BASE.APP.TASK", "FUNCTION", "The task decorator."),
            ("KOMBU", "DEPENDENCY", "A messaging library."),
        ])
        self.assertEqual(report.belongs_to_added, 0)
        self.assertEqual(len(relationships), 0)

    def test_an_edge_the_model_already_emitted_is_not_duplicated(self):
        _, relationships, report = self.ground(
            [("CELERY-V2-5-5", "REPOSITORY", "The task queue."),
             ("CELERY/APP/BASE.PY", "MODULE", "Base.")],
            [("CELERY/APP/BASE.PY", "CELERY-V2-5-5", "BELONGS_TO", 10.0)],
        )
        self.assertEqual(report.belongs_to_added, 0)
        self.assertEqual(len(relationships), 1)

    def test_the_models_edge_counts_in_either_direction(self):
        _, _, report = self.ground(
            [("CELERY-V2-5-5", "REPOSITORY", "The task queue."),
             ("CELERY/APP/BASE.PY", "MODULE", "Base.")],
            [("CELERY-V2-5-5", "CELERY/APP/BASE.PY", "contains", 10.0)],
        )
        self.assertEqual(report.belongs_to_added, 0)

    def test_edges_are_produced_even_when_the_model_emitted_none(self):
        _, relationships, report = self.ground([
            ("CELERY-V2-5-5", "REPOSITORY", "The task queue."),
            ("CELERY/APP/BASE.PY", "MODULE", "Base."),
        ])
        self.assertEqual(report.belongs_to_added, 1)
        self.assertEqual(relationships.at[0, "weight"], graph.BELONGS_TO_WEIGHT)


class TestGround(unittest.TestCase):
    """Both passes together, in the order the workflow runs them."""

    ROWS = [
        ("CELERY.WORKER.STATE", "MODULE", "State."),
        ("CELERY/WORKER/STATE.PY", "MODULE", "Tracks which tasks are running."),
        ("UNKNOWN_REPO", "REPOSITORY", "A repository."),
        ("KOMBU", "DEPENDENCY", "A messaging library."),
    ]

    def test_the_whole_reconciliation_runs_in_one_call(self):
        entities, relationships, report = graph.ground(
            corpus(["celery/worker/state.py"]), entity_frame(self.ROWS), edge_frame()
        )
        self.assertEqual(sorted(entities["title"]),
                         ["CELERY-V2-5-5", "CELERY/WORKER/STATE.PY", "KOMBU"])
        self.assertEqual(report.entities_before, 4)
        self.assertEqual(report.entities_after, 3)
        self.assertEqual(report.relationships_after, 1)

    def test_an_unparseable_corpus_leaves_the_graph_as_extracted(self):
        # No headers means nothing authoritative to reconcile against, and a
        # guess here would be worse than the extraction.
        with self.assertLogs("provenance.graph", level="WARNING"):
            entities, _, report = graph.ground(Corpus(), entity_frame(self.ROWS), edge_frame())
        self.assertEqual(len(entities), 4)
        self.assertEqual(report.entities_after, 0)

    def test_a_second_pass_changes_nothing(self):
        indexed = corpus(["celery/worker/state.py"])
        entities, relationships, _ = graph.ground(indexed, entity_frame(self.ROWS), edge_frame())
        again, edges_again, report = graph.ground(indexed, entities, relationships)
        self.assertEqual(list(again["title"]), list(entities["title"]))
        self.assertEqual(list(again["description"]), list(entities["description"]))
        self.assertEqual(len(edges_again), len(relationships))
        self.assertEqual(report.merged, [])
        self.assertEqual(report.created, [])

    def test_creating_an_entity_does_not_break_the_merge_that_follows(self):
        # Creating a row for an unextracted file adds frequency and
        # text_unit_ids columns the extracted rows never had, and concat
        # fills those with NaN. The repository merge runs next and used to
        # raise on the NaN, which took down the whole workflow.
        entities, _, report = graph.ground(
            corpus(["celery/worker/state.py", "celery/app/base.py"]),
            entity_frame(self.ROWS), edge_frame(),
        )
        self.assertEqual(report.created, ["CELERY/APP/BASE.PY"])
        row = entities.set_index("title").loc["CELERY-V2-5-5"]
        self.assertEqual(row["type"], "REPOSITORY")
        self.assertEqual(int(row["frequency"]), 0)

    def test_the_report_renders_without_a_dataframe(self):
        _, _, report = graph.ground(
            corpus(["celery/worker/state.py", "celery/app/base.py"]),
            entity_frame(self.ROWS), edge_frame(),
        )
        rendered = render(report)
        self.assertIn("CELERY-V2-5-5", rendered)
        self.assertIn("BELONGS_TO added:", rendered)
        self.assertIn("merged:", rendered)


class TestTheGateOnUnfoldableExtensions(unittest.TestCase):
    """Creating an entity for a file the folding could not have matched.

    The repair for a file with no entity is to create one, because the path is
    known and the type is not in doubt. That reasoning holds only while a
    failure to match means something. On a ``.java`` path the key keeps its
    extension and can never meet a model's dotted spelling, so every corpus
    file looks missing, every one gets a duplicate, and the module layer comes
    out at twice its real size with nothing saying so.

    Measured on two-file synthetic corpora before the gate: Java 2 entities to
    5, TypeScript 2 to 5, Python 2 to 3.
    """

    def ground(self, paths, rows, edges=()):
        report = GroundingReport()
        entities, relationships = graph.ground_modules(
            corpus(paths), entity_frame(rows), edge_frame(edges), report
        )
        return entities, relationships, report

    def test_a_gated_file_gets_no_entity_and_the_report_says_why(self):
        entities, _, report = self.ground(
            ["src/main/java/com/example/Foo.java"],
            [("COM.EXAMPLE.FOO", "MODULE", "The Foo class.")],
        )
        self.assertEqual(report.created, [])
        self.assertEqual(sorted(entities["title"]), ["COM.EXAMPLE.FOO"])
        self.assertEqual(len(report.unsupported_files), 1)
        path, reason = report.unsupported_files[0]
        self.assertEqual(path, "src/main/java/com/example/Foo.java")
        self.assertIn(".java", reason)
        self.assertIn("java", reason)

    def test_an_unmapped_extension_is_gated_without_a_language_being_claimed(self):
        _, _, report = self.ground(
            ["cmd/server/main.go"], [("CMD.SERVER.MAIN", "MODULE", "Entrypoint.")],
        )
        self.assertEqual(report.created, [])
        _, reason = report.unsupported_files[0]
        self.assertIn(".go", reason)
        self.assertIn("no language mapping", reason)

    def test_the_skip_is_logged_once_per_reason_not_once_per_file(self):
        """A five-hundred-file repository should say one true thing once."""
        paths = [f"src/main/java/com/example/C{i}.java" for i in range(12)]
        with self.assertLogs("provenance.graph", level="WARNING") as captured:
            _, _, report = self.ground(paths, [("UNRELATED", "MODULE", "x")])
        self.assertEqual(len(captured.records), 1)
        self.assertIn("12 corpus file(s)", captured.output[0])
        self.assertEqual(len(report.unsupported_files), 12)

    def test_only_creation_is_gated_and_matching_still_works(self):
        """A model that spells a gated path exactly still grounds onto it.

        That match is evidence rather than a guess, so the gate leaves it
        alone. What it removes is the inference drawn from a *failure* to
        match, which on an unhandled extension carries no information.
        """
        entities, _, report = self.ground(
            ["src/Foo.java"],
            [("SRC/FOO.JAVA", "MODULE", "The Foo class."),
             ("src.foo.java", "MODULE", "Foo again, another spelling.")],
        )
        self.assertEqual(sorted(entities["title"]), ["SRC/FOO.JAVA"])
        self.assertEqual(report.merged, [("SRC/FOO.JAVA", ["src.foo.java"])])
        self.assertEqual(report.unsupported_files, [])
        self.assertEqual(report.created, [])

    def test_a_mixed_corpus_grounds_the_python_and_gates_the_rest(self):
        """The gate is per file, so one unhandled language does not stop the run.

        A Django or Rails project is mostly templates with a Python or Ruby
        core, and the half the folding handles should still be repaired.
        """
        paths = ["celery/worker/state.py", "celery/app/base.py",
                 "src/App.java", "web/index.html"]
        entities, _, report = self.ground(
            paths,
            # One spelling the folding recognises, and nothing for the rest.
            [("CELERY.WORKER.STATE", "MODULE", "Worker state.")],
        )
        # The recognised one is retitled onto the header spelling, the other
        # Python file is created because a missing .py genuinely is missing,
        # and the two the folding cannot speak for are left alone.
        self.assertEqual(report.created, ["CELERY/APP/BASE.PY"])
        self.assertEqual(
            [p for p, _ in report.unsupported_files],
            ["src/App.java", "web/index.html"],
        )
        self.assertEqual(sorted(entities["title"]),
                         ["CELERY/APP/BASE.PY", "CELERY/WORKER/STATE.PY"])

    def test_the_whole_pass_lands_a_gated_corpus_on_the_control_shape(self):
        """The exit predicate: before + 1, the +1 being the repository.

        ``EX-APP`` comes from the header's ``git_slug``, which is a header fact
        and has nothing to do with suffix stripping, so the gate does not take
        it. A gated language lands on exactly the shape the Python control
        lands on.
        """
        for extension in (".java", ".ts", ".rb", ".go", ".tsx"):
            with self.subTest(extension=extension):
                entities, _, report = graph.ground(
                    corpus([f"pkg/a{extension}", f"pkg/b{extension}"]),
                    entity_frame([("PKG.A", "MODULE", "a"), ("PKG.B", "MODULE", "b")]),
                    edge_frame(),
                )
                self.assertEqual(report.entities_after, report.entities_before + 1)
                self.assertEqual(report.created, ["CELERY-V2-5-5"])
                self.assertIn("CELERY-V2-5-5", set(entities["title"].astype(str)))


if __name__ == "__main__":
    unittest.main()
