"""Every header a checkout produces, compared to the parsers that filled it.

Split out of ``test_data_generation_symbol_sections``, which pins how the
sections render on a handful of constructed sources. This is the other half of
that claim and the half that needs a checkout on disk: the same renderer over
every file, with the oracle recomputed per file and compared against what came
back out of the rendered bytes.

The two halves chain. ``test_provenance_symbols`` proves the kinds fold back to
``declarations_in``; this proves the header carries those kinds without losing
or inventing one on the way through ``generate_code_comment``.
"""

import os
import sys
import types
import unittest
import warnings
from pathlib import Path

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if "pygments" not in sys.modules:
    pygments = types.ModuleType("pygments")
    lexers = types.ModuleType("pygments.lexers")
    lexers.guess_lexer_for_filename = lambda *_: None
    util = types.ModuleType("pygments.util")
    util.ClassNotFound = type("ClassNotFound", (Exception,), {})
    sys.modules.update({"pygments": pygments, "pygments.lexers": lexers, "pygments.util": util})

from pipelines.base.data_generation import generate_code_comment  # noqa: E402  stubs first
from provenance import symbols  # noqa: E402
from provenance.declarations import declarations_in, module_key  # noqa: E402
from provenance.dialects import PYTHON  # noqa: E402
from provenance.imports import imports_in_source, imports_in_tree  # noqa: E402

# Reading the sections back out of a rendered header is the other file's
# business: it is the one that pins the rendering, and two copies of the
# reader could drift from each other without either test noticing.
from .test_data_generation_symbol_sections import parse_sections  # noqa: E402

#: The candidate checkout this runs over. Not part of this repository, so the
#: whole module skips when it is absent.
CANDIDATES = Path(
    os.environ.get("AUGUR_CANDIDATES", Path.home() / "Developer" / "augur-test" / "candidates")
)
CHECKOUT = CANDIDATES / "celery"

EXCLUDE_PARTS = {".git", "__pycache__"}


def candidate_files():
    if not CHECKOUT.is_dir():
        return []
    return [
        path
        for path in sorted(CHECKOUT.rglob("*.py"))
        if not (EXCLUDE_PARTS & set(path.relative_to(CHECKOUT).parts))
    ]


FILES = candidate_files()

#: The split published alongside this change, measured on celery 2.5.5. The
#: 187 files ``ast`` accepts are these two classes together: 173 that declare
#: something and 14 that parse cleanly and declare nothing.
EXPECTED_FILES = 237
EXPECTED_SPLIT = {"ast": 173, "ast-empty": 14, "tree-sitter": 50, "tree-sitter-empty": 0}


def oracle_imports(source: str) -> list:
    """Imports the way ``scan_tree`` has always chosen the scanner.

    Spelled out rather than calling ``imports_in``, so this stays an
    independent check on the combinator the header now goes through.
    """
    try:
        return sorted(imports_in_source(source))
    except SyntaxError:
        return sorted(imports_in_tree(source, PYTHON))


@unittest.skipIf(not FILES, f"no candidate checkout at {CHECKOUT}")
class TestEveryHeaderInTheCheckoutMatchesTheOracle(unittest.TestCase):
    """237 files through the production formatter, compared to the parsers."""

    @classmethod
    def setUpClass(cls):
        # Everything that compiles celery source happens here, under the
        # filter: 237 Python 2 files carry invalid escape sequences in their
        # docstrings and would bury the suite's real warnings.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            cls.cases = {}
            for path in FILES:
                relative = path.relative_to(CHECKOUT).as_posix()
                source = path.read_text(encoding="utf-8", errors="replace")
                metadata = symbols.sections_for(relative, source)
                header = generate_code_comment(
                    metadata=dict(metadata), file_path=relative, config=False,
                    external_metadata=None, git_repo="https://github.com/celery/celery",
                    git_slug="celery-v2-5-5", language="python", multi_repo=False,
                )
                declared, _ = declarations_in(source)
                cls.cases[relative] = {
                    "metadata": metadata,
                    "rendered": parse_sections(header),
                    "declared": declared,
                    "bindings": {n.lower() for n in symbols.symbols_in(source).bindings},
                    "imports": oracle_imports(source),
                }

    def test_the_three_symbol_sections_are_exactly_what_the_oracle_declares(self):
        """Folded, and with bindings added back, the header is ``declarations_in``.

        Bindings are the one kind no section takes, so they are the one thing
        that has to be added back before the comparison. Without them this
        would be a subset check and a dropped class would pass it.

        All 237 files carry the equality now. It used to hold on the 187
        parsed ones and weaken to a one-directional claim on the rest, because
        a precision gate withheld whichever kinds the line scan got wrong
        often enough, leaving a deliberate strict subset in the header. The
        fallback is a parser and the gate is gone, so the recovered files are
        held to the same equality as the parsed ones rather than to a weaker
        promise.
        """
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                declared, bindings = case["declared"], case["bindings"]
                in_header = {
                    name.lower()
                    for section in ("classes", "functions", "methods")
                    for name in case["rendered"][section]
                }
                invented = in_header - declared
                missing = declared - in_header - bindings

                self.assertEqual(
                    declared,
                    in_header | bindings,
                    f"{case['metadata']['symbol_evidence']}: "
                    f"{len(invented)} name(s) in the header the oracle does "
                    f"not declare ({sorted(invented)[:5]}), "
                    f"{len(missing)} declared but not written ({sorted(missing)[:5]})",
                )

    def test_the_dependencies_section_is_exactly_what_the_file_imports(self):
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                self.assertEqual(case["imports"], case["rendered"]["imports"])

    def test_the_package_line_is_the_path_folded_not_a_model_answer(self):
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                self.assertEqual(module_key(relative), case["rendered"]["package"])

    def test_the_sections_reach_the_header_in_source_order(self):
        """Source order, not sorted order. The harness reproduces it that way."""
        for relative, case in self.cases.items():
            with self.subTest(file=relative):
                for section in ("classes", "functions", "methods"):
                    self.assertEqual(
                        case["metadata"].get(section, []), case["rendered"][section]
                    )

    def test_the_parser_split_is_what_the_commit_message_published(self):
        if len(FILES) != EXPECTED_FILES:
            self.skipTest(
                f"{CHECKOUT} has {len(FILES)} files, not the {EXPECTED_FILES} of "
                f"celery 2.5.5 this split was measured on"
            )

        split = dict.fromkeys(EXPECTED_SPLIT, 0)
        for case in self.cases.values():
            split[case["metadata"]["symbol_evidence"]] += 1

        self.assertEqual(EXPECTED_SPLIT, split)


if __name__ == "__main__":
    unittest.main()
