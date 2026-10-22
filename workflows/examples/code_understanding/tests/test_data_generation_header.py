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


class SaveHarness(unittest.TestCase):
    """One row through the real ``save_code_and_metadata_files``, no model."""

    def save(self, extracted, code=None):
        """Runs one row through ``save_code_and_metadata_files``.

        Returns the annotated file's text and the metadata dict handed to the
        flattener, which is what reaches ``_metadata.txt``.
        """
        row = dict(ROW)
        if code is not None:
            row["code"] = code
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

    def header(self, extracted, code=None):
        """The comment block alone, without the source appended below it."""
        return self.save(extracted, code=code)[0].split('"""', 2)[1]


class TestSaveOverwritesRatherThanBackfills(SaveHarness):
    """The call site assigns the five unconditionally, so a wrong one cannot survive."""

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


#: Three Python files that between them exercise both parsers and every
#: section. The Python 2 one is read by tree-sitter, which is roughly a third
#: of celery 2.5.5 and the case where the header is assembled without an AST.
SOURCES = {
    "one binding": ROW["code"],
    "python 3": (
        "import time\n"
        "from celery import current_app\n"
        "\n"
        "SOFTWARE = 'celery'\n"
        "\n"
        "class LocalCache(dict):\n"
        "    def get(self, key):\n"
        "        return dict.get(self, key)\n"
        "\n"
        "def revoke(task_id):\n"
        "    return current_app.control.revoke(task_id)\n"
    ),
    "python 2, tree-sitter": (
        "import sys\n"
        "\n"
        "class Worker(object):\n"
        "    def start(self):\n"
        "        try:\n"
        "            self.run()\n"
        "        except Exception, exc:\n"
        "            print 'failed: %s' % exc\n"
        "\n"
        "def main():\n"
        "    Worker().start()\n"
    ),
}

#: What a model returns now that the prompt asks for one field.
ANSWER = {"purpose": "Holds the set of revoked task ids."}

#: What a model might volunteer anyway. Every key here is one the prompt used
#: to ask for and no longer does, so each is a chance for an unasked answer to
#: reach a header.
VOLUNTEERED = ANSWER | {
    "package": "celery.worker",
    "imports": ["kombu", "billiard"],
    "classes": ["TaskPool"],
    "functions": ["maybe_shutdown"],
    "methods": [{"method_name": "TaskPool.apply_async", "code": "def apply_async(self): ..."}],
}


class TestTheHeaderIsTheSameWhetherOrNotAModelRan(SaveHarness):
    """Phase 5's definition of done, as an assertion rather than a claim.

    The header has one line left that a model produces. Everything above and
    below ``Purpose:`` is stitched from the row or settled by a parser, so a
    run with no endpoint should differ from a run with one in exactly that
    line. If it differs anywhere else, something in the header still depends
    on a model answering, and the reduction in ``data_from_code.yaml`` was not
    safe to make.
    """

    #: Each section is appended as one ``lines`` entry beginning with a
    #: newline, so the blank line above ``Purpose:`` belongs to it and has to
    #: come out with it. Leaving it behind would make an absent section look
    #: like a formatting difference.
    PURPOSE_BLOCK = re.compile(r"\n\n Purpose: [^\n]*")

    def without_purpose(self, text):
        return self.PURPOSE_BLOCK.sub("", text)

    def test_only_the_purpose_line_depends_on_the_model(self):
        for name, code in SOURCES.items():
            with self.subTest(source=name):
                stubbed = self.header(None, code=code)
                answered = self.header(ANSWER, code=code)

                self.assertNotEqual(stubbed, answered, "Purpose: did not render at all")
                self.assertEqual(self.without_purpose(stubbed), self.without_purpose(answered))

    def test_the_purpose_line_is_the_only_line_the_model_adds(self):
        for name, code in SOURCES.items():
            with self.subTest(source=name):
                added = set(self.header(ANSWER, code=code).split("\n")) - set(
                    self.header(None, code=code).split("\n")
                )

                self.assertEqual({f" Purpose: {ANSWER['purpose']}"}, added)

    def test_a_field_the_prompt_stopped_asking_for_cannot_re_enter_by_being_volunteered(self):
        """The prompt is an asset and a model is not bound by it.

        ``save_code_and_metadata_files`` overwrites the parser-settled keys
        after reading the answer, so a model that returns ``methods`` anyway
        is ignored rather than trusted. That is what makes deleting the field
        from the prompt a reduction in tokens and not a reduction in control.
        """
        for name, code in SOURCES.items():
            with self.subTest(source=name):
                self.assertEqual(
                    self.header(ANSWER, code=code), self.header(VOLUNTEERED, code=code)
                )

    def test_the_volunteered_method_body_never_reaches_the_corpus(self):
        """``methods[].code`` was the largest echo; it has to stay out by construction."""
        written, metadata = self.save(VOLUNTEERED, code=SOURCES["python 3"])

        self.assertNotIn("def apply_async", written)
        self.assertNotIn("TaskPool", written)
        self.assertTrue(all(isinstance(name, str) for name in metadata["methods"]))


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
