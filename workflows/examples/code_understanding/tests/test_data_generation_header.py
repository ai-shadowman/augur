"""The five identity fields in a header come from the row, not from the model.

``file_path``, ``git_repo``, ``git_slug``, ``language`` and ``multi_repo`` are
all known to the pipeline before the per-file metadata call. Nothing here
invokes a model because nothing here needs one: the header is a pure function
of the caller's input, so the assertions compare bytes.

What this replaces accepted the model's answer for all five unless it came
back empty, which caught a model that said nothing and passed one that said
something wrong. Two of those wrong answers are not cosmetic. A language the
mappings do not know raises out of ``get_comment_delimiters_for_language``, and
a ``multi_repo`` that is neither ``true`` nor ``false`` fails
``provenance.corpus.HEADER``, which drops the file from the provenance layer
without saying so.
"""

import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from pipelines.base.data_generation import (  # noqa: E402  stubs must land first
    generate_code_comment,
    save_code_and_metadata_files,
)
from provenance.corpus import HEADER  # noqa: E402
from utils import code_utils  # noqa: E402

PROMPT = Path(BASE_DIR) / "assets" / "sdghub" / "code_generation" / "prompts" / "data_from_code.yaml"

#: One row as ``generate_raw_dataset`` builds it.
ROW = {
    "code": "revoked = set()\n",
    "file_path": "celery/worker/state.py",
    "git_repo": "https://github.com/celery/celery",
    "git_slug": "celery-v2-5-5",
    "language": "python",
    "multi_repo": False,
}

#: The four fields that appear in the header text: the value the row carries,
#: and a wrong answer a model could echo back. Every echoed value is non-empty,
#: so the empty-only backfill this replaces would have kept all four.
TEXTUAL_FIELDS = (
    ("file_path", "celery/worker/state.py", "/abs/checkout/celery/worker/state.py"),
    ("git_repo", "https://github.com/celery/celery", "github.com/celery/celery.git"),
    ("git_slug", "celery-v2-5-5", "celery_v2_5_5"),
    ("multi_repo", "false", "N/A"),
)

#: ``language`` is the fifth. It is absent from this table because it is absent
#: from the header text: it selects the comment delimiters instead.
ECHOED_LANGUAGE = "Python 3"

ECHOED = {field: echoed for field, _, echoed in TEXTUAL_FIELDS} | {"language": ECHOED_LANGUAGE}

HEADER_LINE = (
    "This file is located at celery/worker/state.py "
    "from repository url https://github.com/celery/celery, "
    "repository slug celery-v2-5-5, multi_repo false"
)


def comment(metadata, **overrides):
    """Formats a header, taking the five fields from ``ROW`` unless overridden."""
    arguments = {
        "file_path": ROW["file_path"],
        "git_repo": ROW["git_repo"],
        "git_slug": ROW["git_slug"],
        "language": ROW["language"],
        "multi_repo": ROW["multi_repo"],
    }
    arguments.update(overrides)
    return generate_code_comment(metadata=metadata, config=False, external_metadata=None, **arguments)


class TestHeaderIsAPureFunctionOfTheRow(unittest.TestCase):

    def test_bytes_are_identical_whatever_the_model_returned(self):
        purpose = {"purpose": "Holds the set of revoked task ids."}
        expected = (
            '"""' + HEADER_LINE + "\n\n Purpose: Holds the set of revoked task ids." + '"""'
        )

        self.assertEqual(expected, comment(dict(purpose)))
        self.assertEqual(expected, comment(purpose | ECHOED))

    def test_each_textual_field_is_the_row_value_not_the_model_value(self):
        for field, expected, echoed in TEXTUAL_FIELDS:
            with self.subTest(field=field):
                result = comment({field: echoed})
                self.assertIn(expected, result)
                self.assertNotIn(echoed, result)

    def test_an_invented_language_never_reaches_the_delimiter_lookup(self):
        with self.assertRaises(ValueError):
            code_utils.get_comment_delimiters_for_language(ECHOED_LANGUAGE)

        result = comment({"language": ECHOED_LANGUAGE})

        self.assertTrue(result.startswith('"""'), result[:16])
        self.assertTrue(result.endswith('"""'), result[-16:])

    def test_the_language_argument_chooses_the_delimiters(self):
        result = comment({}, language="java")

        self.assertTrue(result.startswith("/*"), result[:16])
        self.assertTrue(result.endswith("*/"), result[-16:])

    def test_multi_repo_renders_as_the_provenance_regex_requires(self):
        for value, rendered in ((False, "false"), (True, "true")):
            with self.subTest(multi_repo=value):
                match = HEADER.search(comment(dict(ECHOED), multi_repo=value))
                self.assertIsNotNone(match, "header did not parse")
                self.assertEqual(rendered, match.group("multi"))

    def test_the_header_parses_and_yields_the_row_values_back(self):
        cases = {"no model answer": {}, "wrong model answer": dict(ECHOED)}
        for name, metadata in cases.items():
            with self.subTest(case=name):
                match = HEADER.search(comment(metadata))
                self.assertIsNotNone(match, "header did not parse")
                self.assertEqual(ROW["file_path"], match.group("path"))
                self.assertEqual(ROW["git_repo"], match.group("repo"))
                self.assertEqual(ROW["git_slug"], match.group("slug"))


class TestSaveOverwritesRatherThanBackfills(unittest.TestCase):
    """The call site assigns the five unconditionally, so a wrong one cannot survive."""

    def save(self, extracted):
        """Runs one row through ``save_code_and_metadata_files``.

        Returns the annotated file's text and the metadata dict handed to the
        flattener, which is what reaches ``_metadata.txt``.
        """
        row = dict(ROW)
        if extracted is not None:
            row["extracted_data"] = json.dumps(extracted)

        df = MagicMock()
        df.iterrows.return_value = [(0, row)]

        captured = {}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def capture(obj, schema=None):
            captured.update(obj)
            return "stub: true"

        with patch("utils.json_utils.flatten_code_metadata", side_effect=capture):
            save_code_and_metadata_files(
                df=df,
                target_path=directory.name,
                git_repo=ROW["git_repo"],
                git_slug=ROW["git_slug"],
                language=ROW["language"],
                config=False,
            )

        written = Path(directory.name, "celery", "worker", "state.txt").read_text(encoding="utf-8")
        return written, captured

    def test_a_wrong_model_answer_reaches_neither_the_header_nor_the_metadata_file(self):
        written, metadata = self.save(ECHOED | {"purpose": "Holds revoked task ids."})

        self.assertIn(HEADER_LINE, written)
        self.assertIn("Purpose: Holds revoked task ids.", written)
        for field, _rendered, echoed in TEXTUAL_FIELDS:
            with self.subTest(field=field):
                self.assertNotIn(echoed, written)
                self.assertEqual(ROW[field], metadata[field])
        self.assertNotIn(ECHOED_LANGUAGE, written)
        self.assertEqual(ROW["language"], metadata["language"])

    def test_no_model_answer_produces_the_same_five_fields(self):
        written, metadata = self.save(None)

        self.assertIn(HEADER_LINE, written)
        self.assertEqual(ROW["code"], written.split('"""', 2)[2].lstrip("\n"))
        for field in ("file_path", "git_repo", "git_slug", "language", "multi_repo"):
            with self.subTest(field=field):
                self.assertEqual(ROW[field], metadata[field])


class TestThePromptNoLongerAsksForThem(unittest.TestCase):
    """Fields 1-5 are gone from the asset, so there is nothing left to echo."""

    #: A numbered top-level entry in the prompt's schema block.
    ENTRY = re.compile(r"^\s*\d+\.\s+\*\*(?P<name>[^*]+)\*\*", re.MULTILINE)

    def requested(self):
        """The field names the system turn asks the model to return."""
        schema = PROMPT.read_text(encoding="utf-8").split("- role: user")[0]
        return [match.group("name") for match in self.ENTRY.finditer(schema)]

    def test_the_schema_block_does_not_request_the_stitched_fields(self):
        asked = self.requested()

        for field in ("file_path", "git_repo", "git_slug", "language", "multi_repo"):
            with self.subTest(field=field):
                self.assertNotIn(field, asked)

    def test_the_schema_block_still_asks_for_what_only_a_model_can_answer(self):
        self.assertIn("purpose", self.requested())

    def test_language_survives_as_context_rather_than_as_an_answer(self):
        self.assertIn("including {{language}}", PROMPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
