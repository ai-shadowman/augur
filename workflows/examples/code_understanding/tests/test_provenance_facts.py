import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from provenance import facts
from provenance.models import (
    BaseImage,
    Layer,
    Package,
    RepoFacts,
    distribution_name,
    normalize_name,
)


def build_tree(root: Path, files: dict[str, str]) -> None:
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")


class TempTreeTestCase(unittest.TestCase):
    def make_tree(self, files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        build_tree(root, files)
        return root


class TestNormalizeName(unittest.TestCase):
    """PEP 503 folding, which is what makes a model's spelling comparable."""

    CASES = [
        ("python_dateutil", "python-dateutil"),
        ("Python-DateUtil", "python-dateutil"),
        ("python.dateutil", "python-dateutil"),
        ("  Kombu  ", "kombu"),
        ("zope.interface", "zope-interface"),
    ]

    def test_equivalent_spellings_fold_together(self):
        for raw, expected in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_name(raw), expected)

    def test_submodules_do_not_fold_into_their_distribution(self):
        self.assertNotEqual(normalize_name("kombu.pools"), normalize_name("kombu"))


class TestDistributionName(unittest.TestCase):
    """An entity title that carries a requirement line rather than a name.

    The short-prompt celery run titled an entity ``COVERAGE>=3.0``. The
    distribution it names is ``coverage``, and reading it any other way lost a
    declared constraint that had in fact been extracted.
    """

    STRIPPED = [
        ("COVERAGE>=3.0", "coverage"),
        ("coverage >= 3.0", "coverage"),
        ("kombu>=2.1.8,<2.2.0", "kombu"),
        ("python-dateutil (>=1.5)", "python-dateutil"),
        ("coverage[toml]>=3.0", "coverage"),
        ("multiprocessing==2.6.2.1", "multiprocessing"),
        ("anyjson!=0.3.1", "anyjson"),
        ("celery~=2.5", "celery"),
    ]

    #: Nothing here opens a version specifier, so nothing may be stripped. A
    #: looser rule would let prose stand in for an entity and quietly satisfy
    #: a constraint the graph never extracted.
    UNTOUCHED = [
        "kombu",
        "zope.interface",
        "coverage of the test suite",
        "python_dateutil",
        "",
    ]

    def test_a_specifier_leaves_the_distribution_behind(self):
        for raw, expected in self.STRIPPED:
            with self.subTest(raw=raw):
                self.assertEqual(distribution_name(raw), expected)

    def test_a_title_without_a_specifier_folds_exactly_as_before(self):
        for raw in self.UNTOUCHED:
            with self.subTest(raw=raw):
                self.assertEqual(distribution_name(raw), normalize_name(raw))


class TestRequirementsDetection(unittest.TestCase):
    """Which files count as pip requirements.

    Syft selects by filename and so misses ``requirements/py25.txt`` entirely.
    This predicate selects by directory as well, which is the whole reason the
    manifest parsers find 45 declarations where Syft finds none.
    """

    CASES = [
        ("requirements.txt", True),
        ("requirements-dev.txt", True),
        ("test-requirements.txt", True),
        ("requirements/default.txt", True),
        ("requirements/py25.txt", True),
        ("requirements/README.txt", True),
        ("docs/notes.txt", False),
        ("requirements.in", False),
        ("setup.py", False),
    ]

    def test_selection_covers_directory_layouts(self):
        for relative, expected in self.CASES:
            with self.subTest(relative=relative):
                self.assertEqual(facts._is_requirements(Path("/repo") / relative), expected)


class TestOwnModules(TempTreeTestCase):
    def test_top_level_packages_are_detected(self):
        root = self.make_tree({
            "celery/__init__.py": "",
            "celery/worker/__init__.py": "",
            "contrib/helper.py": "",   # no __init__.py, still this project's code
            "docs/index.rst": "",      # no Python anywhere beneath it
            ".hidden/__init__.py": "",
        })
        self.assertEqual(facts.own_modules(root), ["celery", "contrib"])

    def test_a_partial_checkout_without_a_top_level_init_still_resolves(self):
        # The indexed corpus can contain celery/worker/*.py without
        # celery/__init__.py. Requiring the init file would leave own_modules
        # empty and every CELERY.* entity would read as a third-party library.
        root = self.make_tree({"celery/worker/state.py": "x = 1\n"})
        self.assertEqual(facts.own_modules(root), ["celery"])

    def test_directories_holding_no_python_are_not_packages(self):
        root = self.make_tree({"scripts/run.sh": "", "assets/logo.png": ""})
        self.assertEqual(facts.own_modules(root), [])

    def test_vendored_trees_do_not_become_own_modules(self):
        root = self.make_tree({"node_modules/pkg/setup.py": "", "build/lib/x.py": ""})
        self.assertEqual(facts.own_modules(root), [])


class TestDiscover(TempTreeTestCase):
    TREE = {
        "requirements/default.txt": "kombu>=2.1.8,<2.2.0\nanyjson>=0.3.1\n",
        "requirements/test.txt": "nose==1.1.2\n",
        "setup.py": "from setuptools import setup\nsetup(install_requires=REQS)\n",
        "Containerfile": "FROM registry.access.redhat.com/ubi9/python-311:latest\n",
        "node_modules/pkg/requirements.txt": "should-not-be-seen==1.0\n",
        ".tox/py27/requirements.txt": "also-not-seen==1.0\n",
    }

    def setUp(self):
        self.root = self.make_tree(self.TREE)
        self.packages, self.images, self.warnings = facts.discover(self.root)

    def test_every_requirements_file_in_the_tree_is_parsed(self):
        self.assertEqual(
            sorted(p.name for p in self.packages), ["anyjson", "kombu", "nose"]
        )

    def test_vendored_trees_are_not_counted_as_our_dependencies(self):
        names = {p.name for p in self.packages}
        self.assertNotIn("should-not-be-seen", names)
        self.assertNotIn("also-not-seen", names)

    def test_base_images_are_collected(self):
        self.assertEqual(len(self.images), 1)
        self.assertEqual(self.images[0].source_file, "Containerfile")

    def test_unrecoverable_setup_py_produces_a_warning(self):
        self.assertTrue(any("computed at runtime" in w for w in self.warnings))


class TestMerge(unittest.TestCase):
    """Syft and the manifest parsers both read a pinned requirements file.

    The manifest record carries the constraint and the verbatim line, so it has
    to win the collision; keeping Syft's bare resolved version would discard
    exactly what an upgrade decision needs.
    """

    def declared(self, **overrides):
        base = {
            "name": "kombu",
            "ecosystem": "pypi",
            "layer": Layer.PACKAGE,
            "source_file": "requirements.txt",
            "constraint": "==2.1.8",
            "version": "2.1.8",
            "declared_line": "kombu==2.1.8",
        }
        base.update(overrides)
        return Package(**base)

    def resolved(self, **overrides):
        base = {
            "name": "kombu",
            "ecosystem": "pypi",
            "layer": Layer.PACKAGE,
            "source_file": "requirements.txt",
            "version": "2.1.8",
        }
        base.update(overrides)
        return Package(**base)

    def test_declared_record_wins_the_collision(self):
        merged = facts._merge([self.declared()], [self.resolved()])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].declared_line, "kombu==2.1.8")

    def test_same_package_in_a_different_file_is_a_separate_declaration(self):
        merged = facts._merge(
            [self.declared(), self.declared(source_file="requirements/test.txt")], []
        )
        self.assertEqual(len(merged), 2)

    def test_syft_only_packages_survive_the_merge(self):
        merged = facts._merge([], [self.resolved(name="glibc", ecosystem="rpm", layer=Layer.OS)])
        self.assertEqual([p.name for p in merged], ["glibc"])


class TestGitFacts(TempTreeTestCase):
    REMOTE = "https://github.com/celery/celery.git"

    def setUp(self):
        self.root = self.make_tree({"README": "demo\n"})
        for command in (
            ["init", "-q"],
            ["config", "user.email", "test@example.com"],
            ["config", "user.name", "Test"],
            ["config", "remote.origin.url", self.REMOTE],
            ["add", "-A"],
            ["commit", "-qm", "initial"],
        ):
            subprocess.run(["git", "-C", str(self.root), *command], check=True,
                           capture_output=True)
        self.facts = facts.git_facts(self.root)

    def test_remote_url_is_read_verbatim(self):
        self.assertEqual(self.facts["git_repo"], self.REMOTE)

    def test_slug_is_derived_from_the_remote(self):
        self.assertIn("celery", self.facts["git_slug"])

    def test_commit_is_a_full_sha(self):
        self.assertEqual(len(self.facts["git_commit"]), 40)

    def test_a_tree_without_git_yields_nulls_rather_than_raising(self):
        plain = self.make_tree({"README": "no git here\n"})
        self.assertEqual(
            facts.git_facts(plain),
            {"git_repo": None, "git_slug": None, "git_commit": None, "git_branch": None},
        )


class TestSlugDerivation(unittest.TestCase):
    """The slug has to match what an indexing run actually used.

    The pipeline builds every slug with ``generate_slug_from_repo``, which is
    ``owner-repo-branch``. A slug derived any other way would have the verifier
    comparing the graph against a name that was never in it.
    """

    CASES = [
        ("https://github.com/celery/celery.git", "main", "celery-celery-main"),
        ("https://github.com/celery/celery", "main", "celery-celery-main"),
        # Detached HEAD is not a branch; the helper's own default stands, which
        # is what a pipeline run given no branch would also have used.
        ("https://github.com/celery/celery.git", "HEAD", "celery-celery-master"),
        ("https://github.com/celery/celery.git", None, "celery-celery-master"),
    ]

    def test_the_slug_matches_the_pipelines_own_derivation(self):
        for remote, branch, expected in self.CASES:
            with self.subTest(remote=remote, branch=branch):
                self.assertEqual(facts._slug_for(remote, branch), expected)

    def test_an_scp_style_remote_reproduces_the_pipelines_own_mishandling(self):
        """``git@host:owner/repo.git`` is not a URL ``urlparse`` understands.

        ``_extract_git_owner_and_repo`` parses it as a path and keeps the host
        in the owner, so the slug comes out as
        ``git@github.com:celery-celery-main``. That is wrong, and it is also
        what every indexing run produces for an ssh clone. Matching it is the
        point: a "corrected" slug here would be a name the graph never used,
        and the verifier would report a loss that never happened.
        """
        self.assertEqual(
            facts._slug_for("git@github.com:celery/celery.git", "main"),
            "git@github.com:celery-celery-main",
        )

    def test_no_remote_yields_no_slug(self):
        self.assertIsNone(facts._slug_for(None, "main"))

    def test_the_helper_is_the_one_the_pipeline_uses(self):
        # Pins the coupling: if generate_slug_from_repo moves or changes shape,
        # this fails here rather than silently reverting to the local fallback.
        from utils.code_utils import generate_slug_from_repo

        self.assertEqual(
            facts._slug_for("https://github.com/celery/celery.git", "main"),
            generate_slug_from_repo("https://github.com/celery/celery.git", "main"),
        )


class TestRoundTrip(TempTreeTestCase):
    def test_write_then_read_preserves_every_field(self):
        original = RepoFacts(
            root="/repo",
            git_repo="https://github.com/celery/celery.git",
            git_slug="celery",
            git_commit="f21f0f2e",
            git_branch="main",
            multi_repo=True,
            own_modules=["celery"],
            source_files=["celery/worker/state.py"],
            checkout_files=["Makefile", "celery/worker/state.py"],
            declared_symbols={"celery.worker.state": ["localcache", "task_accepted"]},
            warnings=["setup.py: computed at runtime"],
            packages=[
                Package(
                    name="kombu",
                    ecosystem="pypi",
                    layer=Layer.PACKAGE,
                    source_file="requirements/default.txt",
                    constraint="<2.2.0,>=2.1.8",
                    purl="pkg:pypi/kombu",
                    declared_line="kombu>=2.1.8,<2.2.0",
                ),
                Package(
                    name="glibc",
                    ecosystem="rpm",
                    layer=Layer.OS,
                    source_file="var/lib/rpm",
                    version="2.34",
                ),
            ],
            base_images=[BaseImage(reference="ubi9", source_file="Containerfile",
                                   stage=None, line_number=1)],
        )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "facts.json"

        facts.write(original, path)
        restored = facts.read(path)

        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertIs(restored.packages[0].layer, Layer.PACKAGE)
        self.assertEqual(len(restored.packages_for_layer(Layer.OS)), 1)

    def test_written_file_is_valid_json_sorted_for_stable_diffs(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "facts.json"
        facts.write(RepoFacts(root="/repo"), path)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(list(data), sorted(data))


class TestCollect(TempTreeTestCase):
    """End to end over a tiny tree, with Syft off so the run stays offline."""

    def setUp(self):
        self.root = self.make_tree({
            "app/__init__.py": "",
            "app/state.py": "class LocalCache(dict):\n"
                            "    def get(self, key): pass\n"
                            "def task_accepted(): pass\n",
            "requirements.txt": "kombu>=2.1.8,<2.2.0\n",
            "Containerfile": "FROM registry.access.redhat.com/ubi9/python-311:latest\n",
            "build/lib/app/state.py": "",
            ".git/HEAD": "ref: refs/heads/main\n",
        })
        self.facts = facts.collect(self.root, run_syft=False)

    def test_all_three_sources_contribute(self):
        self.assertEqual([p.name for p in self.facts.packages], ["kombu"])
        self.assertEqual(len(self.facts.base_images), 1)
        self.assertEqual(self.facts.own_modules, ["app"])

    def test_skipping_syft_leaves_no_spurious_warning(self):
        self.assertEqual(self.facts.warnings, [])

    def test_both_inventories_are_captured_from_one_walk(self):
        # The symbol inventory is what separates a leaked name from an
        # invented one at the class and function layer, the way source_files
        # does it at the module layer.
        self.assertIn("app/state.py", self.facts.source_files)
        self.assertEqual(
            self.facts.declared_symbols["app.state"],
            ["localcache", "localcache.get", "task_accepted"],
        )

    def test_a_non_python_file_contributes_no_symbols(self):
        # requirements.txt is full of ``name = value`` lines, and reading them
        # as declarations would make real inventions look grounded.
        self.assertNotIn("requirements", self.facts.declared_symbols)
        self.assertIn("requirements.txt", self.facts.source_files)

    def test_the_checkout_inventory_is_every_file_but_version_control(self):
        # source_files drops the Containerfile and the committed build tree;
        # a plan naming either names a real file, so this inventory keeps them.
        self.assertNotIn("Containerfile", self.facts.source_files)
        self.assertEqual(self.facts.checkout_files, [
            "Containerfile", "app/__init__.py", "app/state.py",
            "build/lib/app/state.py", "requirements.txt",
        ])


if __name__ == "__main__":
    unittest.main()
