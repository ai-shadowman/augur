import tempfile
import unittest
from pathlib import Path

from provenance import manifests
from provenance.models import Layer


class ManifestTestCase(unittest.TestCase):
    """Shared scaffolding: write a file into a throwaway tree and parse it."""

    def write(self, relative_path, contents):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return root, path


class TestExactVersion(unittest.TestCase):
    """A version is only reported when the specifier pins exactly one.

    Deriving ``1.5`` from ``>=1.5,<2.0`` would be an invention, which is the
    class of error the whole package exists to catch.
    """

    CASES = [
        ("==2.6.2.1", "2.6.2.1"),
        ("==1.0", "1.0"),
        (">=1.5,<2.0", None),
        (">=2.0", None),
        ("<=3.1", None),
        ("~=1.4", None),
        ("==1.*", None),
        ("==1.0,!=1.0.1", None),
        ("", None),
        (None, None),
    ]

    def test_only_single_equality_resolves(self):
        for specifier, expected in self.CASES:
            with self.subTest(specifier=specifier):
                self.assertEqual(manifests._exact_version(specifier), expected)


class TestParseRequirements(ManifestTestCase):
    REQUIREMENTS = "\n".join([
        "# a comment",
        "",
        "kombu>=2.1.8,<2.2.0",
        "python-dateutil>=1.5,<2.0",
        "multiprocessing==2.6.2.1",
        "anyjson>=0.3.1",
        "-r other.txt",
        "-e .",
    ])

    def setUp(self):
        self.root, path = self.write("requirements/default.txt", self.REQUIREMENTS)
        self.packages = manifests.parse_requirements(path, self.root)
        self.by_name = {p.name: p for p in self.packages}

    def test_named_requirements_are_recovered(self):
        self.assertEqual(
            sorted(self.by_name),
            ["anyjson", "kombu", "multiprocessing", "python-dateutil"],
        )

    def test_constraint_is_preserved_and_range_yields_no_version(self):
        # The parser sorts specifier clauses, so the constraint comes back in a
        # canonical order rather than the order the file used. That is why the
        # verbatim line is carried separately.
        kombu = self.by_name["kombu"]
        self.assertEqual(kombu.constraint, "<2.2.0,>=2.1.8")
        self.assertIsNone(kombu.version)
        self.assertEqual(kombu.purl, "pkg:pypi/kombu")

    def test_exact_pin_resolves_to_a_version(self):
        pinned = self.by_name["multiprocessing"]
        self.assertEqual(pinned.version, "2.6.2.1")
        self.assertEqual(pinned.purl, "pkg:pypi/multiprocessing@2.6.2.1")

    def test_verbatim_line_is_kept_because_parsers_reorder_specifiers(self):
        self.assertEqual(self.by_name["kombu"].declared_line, "kombu>=2.1.8,<2.2.0")

    def test_source_file_is_relative_to_the_repository_root(self):
        self.assertEqual(self.by_name["kombu"].source_file, "requirements/default.txt")

    def test_everything_lands_on_the_package_layer(self):
        self.assertTrue(all(p.layer is Layer.PACKAGE for p in self.packages))

    def test_nested_includes_are_not_followed(self):
        # Discovery walks the whole tree and will reach other.txt on its own.
        # Following -r here as well would double-count every shared requirement.
        other = self.root / "requirements" / "other.txt"
        other.write_text("redis>=2.4.4\n", encoding="utf-8")
        again = manifests.parse_requirements(self.root / "requirements/default.txt", self.root)
        self.assertNotIn("redis", {p.name for p in again})

    def test_unparseable_file_returns_empty_rather_than_raising(self):
        root, path = self.write("requirements.txt", "--this-is-not-a-flag\n")
        self.assertEqual(manifests.parse_requirements(path, root), [])


class TestParsePyproject(ManifestTestCase):
    PEP621 = """
[project]
name = "demo"
dependencies = ["litellm", "pathspec>=1.1.1,<2", "pandas==2.2.0"]
"""

    POETRY = """
[tool.poetry.dependencies]
python = "^3.11"
requests = "^2.31"
pandas = { version = "==2.2.0", extras = ["performance"] }
local = { path = "../local" }
"""

    def test_pep621_dependencies(self):
        root, path = self.write("pyproject.toml", self.PEP621)
        by_name = {p.name: p for p in manifests.parse_pyproject(path, root)}
        self.assertEqual(sorted(by_name), ["litellm", "pandas", "pathspec"])
        self.assertIsNone(by_name["litellm"].constraint)
        self.assertEqual(by_name["pathspec"].constraint, "<2,>=1.1.1")
        self.assertEqual(by_name["pandas"].version, "2.2.0")

    def test_poetry_dependencies_including_the_table_form(self):
        root, path = self.write("pyproject.toml", self.POETRY)
        by_name = {p.name: p for p in manifests.parse_pyproject(path, root)}
        self.assertEqual(sorted(by_name), ["local", "pandas", "requests"])
        self.assertEqual(by_name["pandas"].version, "2.2.0")
        self.assertIsNone(by_name["local"].constraint)

    def test_python_itself_is_not_a_dependency(self):
        root, path = self.write("pyproject.toml", self.POETRY)
        self.assertNotIn("python", {p.name for p in manifests.parse_pyproject(path, root)})

    def test_malformed_toml_returns_empty_rather_than_raising(self):
        root, path = self.write("pyproject.toml", "[project\nbroken")
        self.assertEqual(manifests.parse_pyproject(path, root), [])


class TestParseSetupPy(ManifestTestCase):
    LITERAL = """
from setuptools import setup
setup(name="demo", install_requires=["kombu>=2.1.8", "anyjson==0.3.1"])
"""

    COMPUTED = """
from setuptools import setup
install_requires = parse_requirements("requirements/default.txt")
setup(name="celery", install_requires=install_requires)
"""

    PYTHON2 = """
from setuptools import setup
print "building"
setup(name="old", install_requires=["kombu"])
"""

    def test_literal_list_is_recovered(self):
        root, path = self.write("setup.py", self.LITERAL)
        packages, warnings = manifests.parse_setup_py(path, root)
        by_name = {p.name: p for p in packages}
        self.assertEqual(sorted(by_name), ["anyjson", "kombu"])
        self.assertEqual(by_name["anyjson"].version, "0.3.1")
        self.assertEqual(warnings, [])

    def test_runtime_computed_requires_warns_instead_of_reporting_none(self):
        root, path = self.write("setup.py", self.COMPUTED)
        packages, warnings = manifests.parse_setup_py(path, root)
        self.assertEqual(packages, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("computed at runtime", warnings[0])

    def test_python2_source_warns_rather_than_raising(self):
        root, path = self.write("setup.py", self.PYTHON2)
        packages, warnings = manifests.parse_setup_py(path, root)
        self.assertEqual(packages, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("could not parse as Python 3", warnings[0])

    def test_setup_py_is_never_executed(self):
        # If the AST walk ever became an import or an exec, this would raise.
        root, path = self.write("setup.py", "raise SystemExit('executed')\n")
        self.assertEqual(manifests.parse_setup_py(path, root), ([], []))


class TestParseContainerfile(ManifestTestCase):
    MULTISTAGE = "\n".join([
        "FROM registry.access.redhat.com/ubi9/python-311:latest AS builder",
        "RUN pip install -r requirements.txt",
        "FROM --platform=linux/amd64 registry.access.redhat.com/ubi9/ubi-minimal:9.4",
        "COPY --from=builder /opt /opt",
    ])

    def test_base_images_are_recovered_with_their_line_numbers(self):
        root, path = self.write("Containerfile", self.MULTISTAGE)
        images = manifests.parse_containerfile(path, root)
        self.assertEqual(
            [(i.reference, i.line_number) for i in images],
            [
                ("registry.access.redhat.com/ubi9/python-311:latest", 1),
                ("registry.access.redhat.com/ubi9/ubi-minimal:9.4", 3),
            ],
        )

    def test_platform_flag_is_not_mistaken_for_the_reference(self):
        root, path = self.write("Containerfile", self.MULTISTAGE)
        references = [i.reference for i in manifests.parse_containerfile(path, root)]
        self.assertNotIn("--platform=linux/amd64", references)

    def test_earlier_stages_and_scratch_are_not_base_images(self):
        body = "FROM ubi9 AS build\nFROM build AS test\nFROM scratch\n"
        root, path = self.write("Dockerfile", body)
        images = manifests.parse_containerfile(path, root)
        self.assertEqual([i.reference for i in images], ["ubi9"])


if __name__ == "__main__":
    unittest.main()
