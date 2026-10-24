"""The three hops that get declared facts from a checkout into the graph.

capture at clone time, consolidate at index time, stitch inside the pipeline.
GraphRAG itself is only installed on the indexing image, so the workflow and
the registration are exercised against stand-ins that carry the same contract:
``load_table_from_storage``/``write_table_to_storage`` for the workflow, and
``PipelineFactory``'s class-level registries for the install.
"""

import asyncio
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from provenance import facts as facts_module
from provenance import pipeline
from provenance.models import Layer, Package, RepoFacts

STANDARD = [
    "load_input_documents",
    "create_base_text_units",
    "create_final_documents",
    "extract_graph",
    "finalize_graph",
    "extract_covariates",
    "create_communities",
    "create_final_text_units",
    "create_community_reports",
    "generate_text_embeddings",
]


def package(name, constraint, source_file="requirements.txt", declared_line=None):
    return Package(
        name=name,
        ecosystem="pypi",
        layer=Layer.PACKAGE,
        source_file=source_file,
        constraint=constraint,
        declared_line=declared_line,
    )


def frame(rows):
    import pandas as pd

    return pd.DataFrame(rows, columns=["title", "type", "description"])


def edges(rows=()):
    import pandas as pd

    return pd.DataFrame(list(rows), columns=["source", "target", "description", "weight"])


def documents(texts=()):
    import pandas as pd

    return pd.DataFrame({"text": list(texts), "title": [f"d{i}" for i, _ in enumerate(texts)]})


def header(path, slug="celery-v2-5-5", runtime=None):
    """A document header in the form data generation writes it."""
    text = (f'"""This file is located at {path} from repository url '
            f"https://github.com/celery/celery.git, repository slug {slug}, "
            "multi_repo false")
    if runtime:
        text += f", runtime {runtime}"
    return text + '"""\n'


class TempDirTestCase(unittest.TestCase):
    def tempdir(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()


class TestCapture(TempDirTestCase):
    """What data generation writes while the clone is still on disk."""

    def setUp(self):
        self.checkout = self.tempdir() / "repo"
        (self.checkout / "app").mkdir(parents=True)
        (self.checkout / "app" / "__init__.py").write_text("")
        (self.checkout / "requirements.txt").write_text(
            "kombu>=2.1.8,<2.2.0\nanyjson==0.3.1\n", encoding="utf-8"
        )
        self.target = self.tempdir() / "target"

    def test_the_sidecar_lands_where_the_code_metadata_goes(self):
        path = pipeline.capture(self.checkout, self.target, run_syft=False)
        self.assertEqual(path, self.target / pipeline.FACTS_FILENAME)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(p["name"] for p in data["packages"]), ["anyjson", "kombu"]
        )

    def test_the_target_directory_need_not_exist_yet(self):
        # prepare_environment removes target_path immediately before the clone,
        # so capture is the first thing to write into it.
        self.assertFalse(self.target.exists())
        pipeline.capture(self.checkout, self.target, run_syft=False)
        self.assertTrue((self.target / pipeline.FACTS_FILENAME).exists())

    def test_an_extraction_failure_does_not_take_down_data_generation(self):
        # Losing provenance costs the declared constraints. Raising here would
        # cost the whole run, including the clone that was just paid for.
        with mock.patch.object(facts_module, "collect", side_effect=OSError("disk")):
            self.assertIsNone(pipeline.capture(self.checkout, self.target))
        self.assertFalse((self.target / pipeline.FACTS_FILENAME).exists())


class TestMergeFacts(unittest.TestCase):
    def records(self):
        return [
            RepoFacts(
                root="/a", git_repo="https://github.com/celery/celery.git",
                git_slug="celery-celery-main", own_modules=["celery"],
                imported_names=["kombu"], warnings=["a"],
                packages=[package("kombu", ">=2.1.8")],
                source_files=["celery/app/base.py", "shared/compat.py"],
                checkout_files=["celery/app/base.py", "setup.py"],
                declared_symbols={
                    "celery.app.base": ["app"],
                    "shared.compat": ["bytes_to_str"],
                },
            ),
            RepoFacts(
                root="/b", git_repo="https://github.com/celery/kombu.git",
                git_slug="celery-kombu-main", own_modules=["kombu"],
                imported_names=["amqp", "kombu"], warnings=["b"],
                packages=[package("amqp", ">=1.0")],
                source_files=["kombu/entity.py", "shared/compat.py"],
                declared_symbols={
                    "kombu.entity": ["queue"],
                    "shared.compat": ["text_t"],
                },
            ),
        ]

    def test_a_single_record_passes_through_untouched(self):
        only = self.records()[:1]
        self.assertIs(pipeline.merge_facts(only, root="/x"), only[0])

    def test_declarations_from_every_repository_survive(self):
        merged = pipeline.merge_facts(self.records(), root="/x")
        self.assertEqual(sorted(p.name for p in merged.packages), ["amqp", "kombu"])

    def test_the_git_identity_is_dropped_rather_than_guessed(self):
        # Two remotes, one graph. Keeping either would attach the wrong URL to
        # every repository entity but one.
        merged = pipeline.merge_facts(self.records(), root="/x")
        self.assertIsNone(merged.git_repo)
        self.assertIsNone(merged.git_slug)
        self.assertTrue(merged.multi_repo)

    def test_module_and_import_names_are_pooled_without_duplicates(self):
        merged = pipeline.merge_facts(self.records(), root="/x")
        self.assertEqual(merged.own_modules, ["celery", "kombu"])
        self.assertEqual(merged.imported_names, ["amqp", "kombu"])

    def test_warnings_from_both_repositories_are_kept(self):
        self.assertEqual(pipeline.merge_facts(self.records(), root="/x").warnings, ["a", "b"])

    def test_both_inventories_survive_the_merge(self):
        # These are what tell a leaked name from an invented one. Dropping
        # them left every multi-repo index unable to make that call, and
        # silently: the counts still added up, they just all said "invented".
        merged = pipeline.merge_facts(self.records(), root="/x")
        self.assertEqual(
            merged.source_files,
            ["celery/app/base.py", "kombu/entity.py", "shared/compat.py"],
        )
        self.assertEqual(sorted(merged.declared_symbols), [
            "celery.app.base", "kombu.entity", "shared.compat",
        ])

    def test_the_checkout_inventory_is_not_pooled(self):
        # Its paths are relative to one tree: pooled, setup.py from kombu's
        # checkout would confirm a plan naming setup.py in celery's.
        self.assertEqual(pipeline.merge_facts(self.records(), root="/x").checkout_files, [])

    def test_a_module_both_repositories_declare_keeps_every_symbol(self):
        # A vendored file appearing in two checkouts is one module key with
        # two symbol sets, so the second must extend the first rather than
        # replace it.
        merged = pipeline.merge_facts(self.records(), root="/x")
        self.assertEqual(merged.declared_symbols["shared.compat"], ["bytes_to_str", "text_t"])


class TestConsolidate(TempDirTestCase):
    def setUp(self):
        self.root = self.tempdir()
        self.input = self.root / "input"
        self.input.mkdir()

    def sidecar(self, relative, facts):
        path = self.input / relative / pipeline.FACTS_FILENAME
        facts_module.write(facts, path)
        return path

    def test_one_sidecar_becomes_the_root_facts_file(self):
        self.sidecar("celery", RepoFacts(root="/repo", packages=[package("kombu", ">=2.1.8")]))
        destination = pipeline.consolidate(self.input, self.root)
        self.assertEqual(destination, self.root / pipeline.FACTS_FILENAME)
        self.assertEqual(len(facts_module.read(destination).packages), 1)

    def test_sidecars_are_removed_from_the_indexed_corpus(self):
        path = self.sidecar("celery", RepoFacts(root="/repo"))
        pipeline.consolidate(self.input, self.root)
        self.assertFalse(path.exists())

    def test_several_sidecars_merge_into_one_multi_repo_record(self):
        self.sidecar("celery", RepoFacts(root="/a", git_repo="https://host/a.git",
                                         packages=[package("kombu", ">=2.1.8")]))
        self.sidecar("kombu", RepoFacts(root="/b", git_repo="https://host/b.git",
                                        packages=[package("amqp", ">=1.0")]))
        merged = facts_module.read(pipeline.consolidate(self.input, self.root))
        self.assertTrue(merged.multi_repo)
        self.assertEqual(len(merged.packages), 2)
        self.assertIsNone(merged.git_repo)

    def test_a_single_repo_sidecar_is_marked_multi_when_the_run_is(self):
        # run_multi_repo can index one repository. The caller knows that, and
        # the flag is what suppresses the repository URL downstream.
        self.sidecar("celery", RepoFacts(root="/a", git_repo="https://host/a.git"))
        merged = facts_module.read(pipeline.consolidate(self.input, self.root, multi_repo=True))
        self.assertTrue(merged.multi_repo)

    def test_no_sidecars_is_not_an_error(self):
        self.assertIsNone(pipeline.consolidate(self.input, self.root))
        self.assertFalse((self.root / pipeline.FACTS_FILENAME).exists())

    def test_every_sidecar_unreadable_leaves_no_facts_file_behind(self):
        bad = self.input / "broken" / pipeline.FACTS_FILENAME
        bad.parent.mkdir()
        bad.write_text("{ not json", encoding="utf-8")
        with self.assertLogs("provenance.pipeline", level="WARNING"):
            self.assertIsNone(pipeline.consolidate(self.input, self.root))
        self.assertFalse((self.root / pipeline.FACTS_FILENAME).exists())

    def test_a_corrupt_sidecar_is_skipped_and_the_rest_still_merge(self):
        bad = self.input / "broken" / pipeline.FACTS_FILENAME
        bad.parent.mkdir()
        bad.write_text("{ not json", encoding="utf-8")
        self.sidecar("celery", RepoFacts(root="/a", packages=[package("kombu", ">=2.1.8")]))
        with self.assertLogs("provenance.pipeline", level="WARNING"):
            merged = facts_module.read(pipeline.consolidate(self.input, self.root))
        self.assertEqual(len(merged.packages), 1)


class TestLoadFacts(TempDirTestCase):
    def setUp(self):
        self.root = self.tempdir()

    def test_a_missing_file_yields_none_rather_than_raising(self):
        self.assertIsNone(pipeline.load_facts(self.root))

    def test_the_environment_override_wins(self):
        elsewhere = self.root / "somewhere" / "facts.json"
        facts_module.write(RepoFacts(root="/repo", packages=[package("kombu", ">=2.1.8")]),
                           elsewhere)
        with mock.patch.dict("os.environ", {pipeline.FACTS_ENV: str(elsewhere)}):
            loaded = pipeline.load_facts(self.root)
        self.assertEqual(len(loaded.packages), 1)

    def test_an_unreadable_file_is_a_warning_not_a_failed_index(self):
        (self.root / pipeline.FACTS_FILENAME).write_text("{ not json", encoding="utf-8")
        with self.assertLogs("provenance.pipeline", level="WARNING"):
            self.assertIsNone(pipeline.load_facts(self.root))


class TestPublish(TempDirTestCase):
    def setUp(self):
        self.root = self.tempdir()
        self.output = self.root / "output"
        self.output.mkdir()

    def test_the_facts_travel_with_the_uploaded_directory(self):
        facts_module.write(RepoFacts(root="/repo", checkout_files=["setup.py"]),
                           self.root / pipeline.FACTS_FILENAME)
        destination = pipeline.publish(self.root, self.output)
        self.assertEqual(destination, self.output / pipeline.FACTS_FILENAME)
        self.assertEqual(pipeline.load_facts(self.output).checkout_files, ["setup.py"])

    def test_no_facts_publishes_nothing(self):
        self.assertIsNone(pipeline.publish(self.root, self.output))
        self.assertEqual(list(self.output.iterdir()), [])


class FakeStorage:
    """Stands in for GraphRAG's PipelineStorage, keyed the same way."""

    def __init__(self, tables=None):
        self.tables = dict(tables or {})
        self.writes = []


class WorkflowTestCase(TempDirTestCase):
    """Runs ``run_workflow`` with GraphRAG's storage helpers substituted."""

    ROWS = [
        ("CELERY", "REPOSITORY", "CELERY is the task queue. Repository hosted at celery."),
        ("KOMBU", "dependency", "KOMBU is a third-party messaging library."),
    ]

    EDGES: tuple = ()

    FACTS = RepoFacts(
        root="/repo",
        git_repo="https://github.com/celery/celery.git",
        packages=[package("kombu", "<2.2.0,>=2.1.8", "requirements/default.txt",
                          "kombu>=2.1.8,<2.2.0")],
    )

    #: Headers to put in the ``documents`` table. Empty by default so the
    #: grounding pass stands down and these cases stay about the stitch;
    #: :class:`TestRunWorkflowGrounding` supplies real ones.
    DOCUMENTS: tuple = ()

    def setUp(self):
        self.root = self.tempdir()
        self.storage = FakeStorage({
            "entities": frame(self.ROWS),
            "relationships": edges(self.EDGES),
            "documents": documents(self.DOCUMENTS),
        })
        self.config = types.SimpleNamespace(root_dir=str(self.root), workflows=None)
        self.context = types.SimpleNamespace(output_storage=self.storage)

        async def load_table_from_storage(name, storage):
            return storage.tables[name]

        async def write_table_to_storage(table, name, storage):
            storage.tables[name] = table
            storage.writes.append(name)

        workflow = types.ModuleType("graphrag.index.typing.workflow")

        class WorkflowFunctionOutput:
            def __init__(self, result=None, stop=False):
                self.result = result
                self.stop = stop

        workflow.WorkflowFunctionOutput = WorkflowFunctionOutput
        storage_module = types.ModuleType("graphrag.utils.storage")
        storage_module.load_table_from_storage = load_table_from_storage
        storage_module.write_table_to_storage = write_table_to_storage

        patcher = mock.patch.dict(sys.modules, {
            "graphrag.index.typing.workflow": workflow,
            "graphrag.utils.storage": storage_module,
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_stage(self):
        return asyncio.run(pipeline.run_workflow(self.config, self.context))


class TestRunWorkflow(WorkflowTestCase):
    """Without headers, so these cases stay about the stitch."""

    def test_headers_that_do_not_parse_are_logged_and_the_stitch_still_runs(self):
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        with self.assertLogs("provenance.pipeline", level="WARNING") as logged:
            self.run_stage()
        self.assertTrue(any("not grounded" in line for line in logged.output))
        self.assertEqual(self.storage.writes, ["entities"])

    def test_the_entity_table_is_written_back_stitched(self):
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        output = self.run_stage()
        self.assertEqual(self.storage.writes, ["entities"])
        descriptions = dict(zip(self.storage.tables["entities"]["title"],
                                self.storage.tables["entities"]["description"],
                                strict=True))
        self.assertTrue(descriptions["KOMBU"].endswith("Version: >=2.1.8,<2.2.0"))
        self.assertIn("https://github.com/celery/celery.git", descriptions["CELERY"])
        self.assertEqual(output.result["stitch"]["changed"], 2)

    def test_the_runs_own_vocabulary_is_what_entities_are_held_to(self):
        # Read off the config the run used rather than restated in the
        # provenance package, so the two cannot drift. KOMBU is typed
        # "dependency", which is the type that split into library and
        # stdlib_module and that no run has configured since.
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        self.config.extract_graph = types.SimpleNamespace(
            entity_types=["module", "class", "function", "package", "library",
                          "stdlib_module"],
        )
        output = self.run_stage()
        self.assertEqual(output.result["stitch"]["off_vocabulary"], {"dependency": 1})

    def test_a_config_without_an_extraction_section_skips_the_check(self):
        # Nothing in this package should require a config shape it does not
        # own; a missing section costs the count, not the stitch.
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        output = self.run_stage()
        self.assertEqual(output.result["stitch"]["off_vocabulary"], {})

    def test_no_facts_leaves_the_table_alone_instead_of_failing_the_index(self):
        output = self.run_stage()
        self.assertEqual(self.storage.writes, [])
        self.assertIsNone(output.result)

    def test_a_second_pass_writes_nothing(self):
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        self.run_stage()
        self.storage.writes.clear()
        output = self.run_stage()
        self.assertEqual(self.storage.writes, [])
        self.assertEqual(output.result["stitch"]["changed"], 0)


class TestRunWorkflowGrounding(WorkflowTestCase):
    """With real headers, which is the shape of an actual indexing run.

    The headers carry a slug and a runtime the derived facts do not have, and
    that disagreement is the one the verifier used to lose: ``facts`` derives
    ``celery`` from the remote, data generation wrote ``celery-v2-5-5`` into
    every document. The header is what the model was shown, so it wins.
    """

    ROWS = [
        ("UNKNOWN_REPO", "REPOSITORY", "A repository."),
        ("KOMBU", "dependency", "KOMBU is a third-party messaging library."),
        ("CELERY.WORKER.STATE", "module", "Worker state tracking."),
    ]

    EDGES = (
        ("CELERY.WORKER.STATE", "KOMBU", "imports", 8.0),
    )

    DOCUMENTS = (
        header("celery/worker/state.py", runtime="Python 2.7"),
    )

    def test_both_tables_are_grounded_before_the_stitch_writes(self):
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        output = self.run_stage()
        titles = set(self.storage.tables["entities"]["title"])
        self.assertEqual(titles, {"CELERY-V2-5-5", "KOMBU", "CELERY/WORKER/STATE.PY",
                                  "CELERY", "CELERY.WORKER"})
        self.assertEqual(output.result["grounding"]["repository"], "CELERY-V2-5-5")
        self.assertIn("relationships", self.storage.writes)

    def test_the_header_identity_reaches_the_repository_description(self):
        facts_module.write(self.FACTS, self.root / pipeline.FACTS_FILENAME)
        self.run_stage()
        descriptions = dict(zip(self.storage.tables["entities"]["title"],
                                self.storage.tables["entities"]["description"],
                                strict=True))
        self.assertIn("slug celery-v2-5-5", descriptions["CELERY-V2-5-5"])
        self.assertIn("Runtime stack: Python 2.7", descriptions["CELERY-V2-5-5"])

    def test_a_slug_the_headers_disagree_with_is_reported_not_swallowed(self):
        # Two slugs for one repository is worth seeing, even though the
        # header is the one that gets used.
        facts = RepoFacts(root="/repo", git_repo=self.FACTS.git_repo, git_slug="celery",
                          packages=self.FACTS.packages)
        facts_module.write(facts, self.root / pipeline.FACTS_FILENAME)
        with self.assertLogs("provenance.pipeline", level="WARNING") as logged:
            self.run_stage()
        self.assertTrue(any("git_slug: headers say 'celery-v2-5-5'" in line
                            for line in logged.output))

    def test_grounding_runs_even_with_no_facts_to_stitch(self):
        # The headers are in the corpus, not the sidecar, so a missing
        # sidecar costs the versions and nothing else.
        output = self.run_stage()
        self.assertIn("CELERY-V2-5-5", set(self.storage.tables["entities"]["title"]))
        self.assertNotIn("stitch", output.result)


class TestRunWorkflowSymbols(WorkflowTestCase):
    """The symbol layer reads the document bodies, so the stage must pass them.

    Everything else the stage grounds works off the headers alone, which means
    dropping ``documents`` from the ``ground`` call would leave every other
    case here green while silently turning the symbol repair off in
    production. This is the one test that notices.
    """

    ROWS = [
        ("CELERY.APP.TASK_ACCEPTED", "function", "Marks a task as accepted."),
    ]

    DOCUMENTS = (
        header("celery/worker/state.py") + "def task_accepted(): pass\n",
    )

    def test_a_misattributed_symbol_is_refiled_by_the_stage(self):
        output = self.run_stage()
        self.assertIn(
            "CELERY.WORKER.STATE.TASK_ACCEPTED",
            set(self.storage.tables["entities"]["title"]),
        )
        self.assertEqual(
            output.result["grounding"]["symbols_refiled"],
            [("CELERY.APP.TASK_ACCEPTED", "CELERY.WORKER.STATE.TASK_ACCEPTED")],
        )


class TestReconcile(unittest.TestCase):
    """Where the facts and the headers disagree, the header wins."""

    def facts(self, **kwargs):
        return RepoFacts(root="/repo", **kwargs)

    def shown(self, **kwargs):
        defaults = {"git_repo": None, "git_slug": None, "runtime": None}
        return types.SimpleNamespace(**{**defaults, **kwargs})

    def test_the_header_slug_replaces_the_derived_one_and_is_reported(self):
        facts = self.facts(git_slug="celery-celery-master")
        notes = pipeline.reconcile(facts, self.shown(git_slug="celery-v2-5-5"))
        self.assertEqual(facts.git_slug, "celery-v2-5-5")
        self.assertEqual(len(notes), 1)
        self.assertIn("using the header", notes[0])

    def test_a_value_only_the_header_has_is_taken_without_a_note(self):
        # Nothing derives the runtime from a checkout, so the header is not
        # merely authoritative, it is the only source there is.
        facts = self.facts()
        self.assertEqual(pipeline.reconcile(facts, self.shown(runtime="Python 2.7")), [])
        self.assertEqual(facts.runtime, "Python 2.7")

    def test_agreement_produces_no_note_and_no_change(self):
        facts = self.facts(git_repo="https://host/a.git")
        self.assertEqual(
            pipeline.reconcile(facts, self.shown(git_repo="https://host/a.git")), []
        )
        self.assertEqual(facts.git_repo, "https://host/a.git")

    def test_a_header_with_nothing_to_say_leaves_the_derived_value_alone(self):
        facts = self.facts(git_slug="celery-celery-master")
        self.assertEqual(pipeline.reconcile(facts, self.shown()), [])
        self.assertEqual(facts.git_slug, "celery-celery-master")


class FakePipelineFactory:
    workflows: dict = {}
    pipelines: dict = {"standard": list(STANDARD)}

    @classmethod
    def register(cls, name, workflow):
        cls.workflows[name] = workflow


class TestInstall(unittest.TestCase):
    """Where the stage lands in the workflow list, which is the whole point."""

    def setUp(self):
        factory = types.ModuleType("graphrag.index.workflows.factory")
        FakePipelineFactory.workflows = {}
        FakePipelineFactory.pipelines = {"standard": list(STANDARD)}
        factory.PipelineFactory = FakePipelineFactory
        patcher = mock.patch.dict(
            sys.modules, {"graphrag.index.workflows.factory": factory}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = types.SimpleNamespace(root_dir="/root", workflows=None)

    def test_the_stage_runs_directly_after_extract_graph(self):
        self.assertTrue(pipeline.install(self.config))
        names = self.config.workflows
        self.assertEqual(
            names.index(pipeline.WORKFLOW_NAME), names.index("extract_graph") + 1
        )

    def test_the_stage_precedes_every_consumer_of_the_graph(self):
        # The reason for the position. finalize_graph regenerates ids, degrees
        # and the layout and drops duplicate titles, so grounding before it
        # gets all of that applied to the corrected graph for free. The other
        # three read the descriptions the stitch rewrites, so running after
        # any of them leaves communities, reports and vectors describing text
        # that is no longer in the table.
        pipeline.install(self.config)
        names = self.config.workflows
        position = names.index(pipeline.WORKFLOW_NAME)
        for consumer in (
            "finalize_graph",
            "create_communities",
            "create_community_reports",
            "generate_text_embeddings",
        ):
            self.assertLess(position, names.index(consumer), msg=consumer)

    def test_the_rest_of_the_pipeline_is_preserved_in_order(self):
        pipeline.install(self.config)
        kept = [n for n in self.config.workflows if n != pipeline.WORKFLOW_NAME]
        self.assertEqual(kept, STANDARD)

    def test_the_workflow_is_registered_under_its_name(self):
        pipeline.install(self.config)
        self.assertIs(
            FakePipelineFactory.workflows[pipeline.WORKFLOW_NAME], pipeline.run_workflow
        )

    def test_an_explicit_workflow_list_in_settings_is_extended_not_replaced(self):
        # create_pipeline prefers config.workflows over the named pipeline, so
        # a project that pins its own list has to be inserted into, not around.
        self.config.workflows = ["extract_graph", "finalize_graph"]
        pipeline.install(self.config)
        self.assertEqual(
            self.config.workflows,
            ["extract_graph", pipeline.WORKFLOW_NAME, "finalize_graph"],
        )

    def test_installing_twice_does_not_duplicate_the_stage(self):
        pipeline.install(self.config)
        first = list(self.config.workflows)
        self.assertTrue(pipeline.install(self.config))
        self.assertEqual(self.config.workflows, first)

    def test_a_pipeline_without_the_anchor_declines_rather_than_guessing(self):
        # A position relative to a workflow that is not there would be made up,
        # and the wrong position is what this whole stage exists to avoid.
        FakePipelineFactory.pipelines["standard"] = ["create_base_text_units", "create_communities"]
        with self.assertLogs("provenance.pipeline", level="WARNING"):
            self.assertFalse(pipeline.install(self.config))
        self.assertIsNone(self.config.workflows)


class TestStandardPipelineIsStillWhatWeThink(unittest.TestCase):
    """Pins STANDARD against the installed GraphRAG, where one is installed.

    The fixtures above assert a position inside a list this repository does not
    own. If an upgrade renames or reorders those workflows, the unit tests keep
    passing against a pipeline that no longer exists, so the real one is read
    here when it is importable.
    """

    def test_the_fixture_matches_the_installed_pipeline(self):
        # Other suites in this repository leave a MagicMock under "graphrag" in
        # sys.modules. Importing through one neither raises ImportError nor
        # yields the real registry, so asserting against it would pass for the
        # wrong reason, which is the failure mode this test exists to catch.
        installed = sys.modules.get("graphrag")
        if installed is not None and not isinstance(installed, types.ModuleType):
            self.skipTest("graphrag in sys.modules is a stand-in, not the package")
        try:
            from graphrag.config.enums import IndexingMethod
            from graphrag.index.workflows.factory import PipelineFactory
        except ImportError:
            self.skipTest("graphrag is only installed on the indexing image")
        self.assertEqual(
            list(PipelineFactory.pipelines[IndexingMethod.Standard]), STANDARD
        )


class TestStageWiring(unittest.TestCase):
    """The call sites, checked by reading them.

    Importing the pipeline stages pulls in Kubeflow, MLflow and the GraphRAG
    client, none of which belong in a unit test run. The contract that matters
    is only that each stage calls its half of this module.
    """

    ROOT = Path(__file__).resolve().parent.parent

    def source(self, relative):
        return (self.ROOT / relative).read_text(encoding="utf-8")

    def test_data_generation_captures_facts_while_the_clone_exists(self):
        text = self.source("pipelines/base/data_generation.py")
        self.assertIn("capture as capture_provenance", text)
        self.assertLess(text.index("clone_from_repo(git_repo"),
                        text.index("capture_provenance(source_path"))

    def test_indexing_consolidates_before_graphrag_reads_the_corpus(self):
        text = self.source("pipelines/base/indexing.py")
        self.assertIn("consolidate as consolidate_provenance", text)
        self.assertLess(text.index("consolidate_provenance("),
                        text.index("run_graphrag(graphrag_source_path)"))

    def test_the_indexer_installs_the_stage_before_building(self):
        text = self.source("pipelines/graphrag.py")
        self.assertIn("install as install_provenance_stitch", text)
        self.assertLess(text.index("install_provenance_stitch(config)"),
                        text.index("graphrag_api.build_index("))


class TestEndToEndSidecar(TempDirTestCase):
    """capture, then the copy indexing performs, then consolidate."""

    def test_facts_survive_the_trip_from_checkout_to_project_root(self):
        work = self.tempdir()
        checkout = work / "source"
        (checkout / "celery").mkdir(parents=True)
        (checkout / "celery" / "__init__.py").write_text("")
        (checkout / "requirements.txt").write_text("kombu>=2.1.8,<2.2.0\n", encoding="utf-8")
        target = work / "target"
        target.mkdir()
        (target / "celery.txt").write_text("code metadata\n", encoding="utf-8")

        pipeline.capture(checkout, target, run_syft=False)

        # What generate_graphrag_index does: copy the metadata tree into input.
        root = work / "graphrag"
        shutil.copytree(target, root / "input", dirs_exist_ok=True)
        pipeline.consolidate(root / "input", root)

        loaded = pipeline.load_facts(root)
        self.assertEqual([p.name for p in loaded.packages], ["kombu"])
        self.assertFalse((root / "input" / pipeline.FACTS_FILENAME).exists())
        self.assertTrue((root / "input" / "celery.txt").exists())


if __name__ == "__main__":
    unittest.main()
