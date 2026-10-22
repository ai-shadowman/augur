"""The decidability table is an argument, so it has to stay checkable.

A table of verdicts is only worth more than the prose it replaced if something
fails when it drifts from the code. These tests do three jobs: keep the shape
honest (a verdict names what settles it, or admits nothing does), keep the
``parsed`` rows pointing at callables that exist, and pin the claims the two
prompt deletions rest on — ``methods[].exceptions`` in Phase 4 and the rest of
the ``methods[]`` object in Phase 5.
"""

import importlib
import json
import re
import unittest
from pathlib import Path

from provenance import decidability as d

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PROMPT = ASSETS / "sdghub" / "code_generation" / "prompts" / "data_from_code.yaml"
SCHEMA = ASSETS / "schemas" / "code_metadata_schema.json"

VERDICTS = (
    d.PARSED,
    d.PARSEABLE,
    d.PARTIAL,
    d.UNDECIDABLE,
    d.NOT_IN_FILE,
    d.NOT_APPLICABLE,
)

#: Verdicts that assert something deterministic settles the field, or part of
#: it. Each must name it; the rest must not pretend to.
SETTLING = (d.PARSED, d.PARSEABLE, d.PARTIAL)


def rows():
    """Every recorded cell as an :class:`~provenance.decidability.Entry`."""
    return [
        d.decide(language, question)
        for language, row in d.TABLE.items()
        for question in row
    ]


class TestTheTableIsWellFormed(unittest.TestCase):
    def test_every_row_carries_a_known_verdict(self):
        for entry in rows():
            with self.subTest(language=entry.language, question=entry.question):
                self.assertIn(entry.verdict, VERDICTS)

    def test_every_row_argues_rather_than_restating_the_verdict(self):
        for entry in rows():
            with self.subTest(language=entry.language, question=entry.question):
                self.assertTrue(entry.reason.strip(), "a verdict with no reason is prose removed")
                self.assertNotIn(entry.verdict, entry.reason.lower())

    def test_a_settling_verdict_names_what_settles_it(self):
        for entry in rows():
            with self.subTest(language=entry.language, question=entry.question):
                if entry.verdict in SETTLING:
                    self.assertTrue(
                        entry.settled_by.strip(),
                        f"{entry.verdict} claims something decides this but does not say what",
                    )
                else:
                    self.assertEqual(
                        "",
                        entry.settled_by,
                        f"{entry.verdict} means nothing settles it, so naming a settler "
                        f"contradicts the verdict",
                    )

    def test_the_load_bearing_reasons_are_not_duplicated(self):
        """A reason reused across questions is filler, and filler hides a guess.

        ``not-applicable`` is exempt: 'this language has no methods' is one
        fact about Ansible, and restating it ten different ways would be the
        worse failure.
        """
        seen: dict[str, tuple[str, str]] = {}
        shared = []
        for entry in rows():
            if entry.verdict == d.NOT_APPLICABLE:
                continue
            here = (entry.language, entry.question)
            if entry.reason in seen:
                shared.append((seen[entry.reason], here, entry.reason))
            seen[entry.reason] = here

        self.assertEqual([], shared, f"{len(shared)} reason(s) reused: {shared}")

    def test_every_question_the_prompt_asks_is_assessed_for_python(self):
        """Python is the only language with a parser here, so it decides the prompt."""
        for question in d.PROMPTED:
            with self.subTest(question=question):
                self.assertNotEqual(d.UNRECORDED, d.decide("python", question).verdict)


class TestTheParsedRowsPointAtRealCode(unittest.TestCase):
    """``parsed`` is a claim about this repository, so it is checkable here.

    ``parseable`` deliberately is not: it says the syntax decides the question
    and nothing here reads it yet, which is the gap Phase 6 closes.
    """

    def test_each_parsed_row_names_a_callable_that_exists(self):
        parsed = [e for e in rows() if e.verdict == d.PARSED]

        self.assertTrue(parsed, "no parsed rows, so this test proves nothing")
        for entry in parsed:
            with self.subTest(question=entry.question, settled_by=entry.settled_by):
                module_path, _, attribute = entry.settled_by.rpartition(".")
                module = importlib.import_module(module_path)

                self.assertTrue(callable(getattr(module, attribute)))

    def test_nothing_is_marked_parsed_for_a_language_with_no_parser(self):
        for language in ("java", "ansible"):
            with self.subTest(language=language):
                verdicts = {d.decide(language, q).verdict for q in d.TABLE[language]}

                self.assertNotIn(d.PARSED, verdicts)


class TestLookupBehaviour(unittest.TestCase):
    def test_an_unassessed_language_is_unrecorded_rather_than_absent(self):
        entry = d.decide("rust", d.EXCEPTIONS)

        self.assertEqual(d.UNRECORDED, entry.verdict)
        self.assertIn("rust", entry.reason)

    def test_the_language_is_matched_case_and_space_insensitively(self):
        self.assertEqual(d.decide("python", d.EXCEPTIONS), d.decide("  PYTHON ", d.EXCEPTIONS))

    def test_an_unknown_question_raises_because_it_is_a_typo_or_a_schema_change(self):
        with self.assertRaises(KeyError) as caught:
            d.decide("python", "complexity")

        self.assertIn("complexity", str(caught.exception))

    def test_unrecorded_refuses_to_license_a_model_call(self):
        """The one answer that must not read as permission."""
        with self.assertRaises(LookupError):
            d.model_justified("rust", d.EXCEPTIONS)

    def test_a_model_is_justified_only_where_the_file_answers_and_no_parser_does(self):
        cases = {
            # (language, question): justified
            ("python", d.PURPOSE): True,       # undecidable: in the file, not mechanical
            ("python", d.EXCEPTIONS): False,   # not-in-file: transitive past this file
            ("python", d.CALLERS): False,      # not-in-file: cross-file by construction
            ("python", d.PARAMETERS): False,   # partial: narrow the field, do not ask it
            ("python", d.METHOD_CODE): False,  # parseable: a slice of a file on disk
            ("python", d.IMPORTS): False,      # parsed today
            ("java", d.EXCEPTIONS): False,     # partial: parse the throws clause instead
            ("java", d.FUNCTIONS): False,      # the language has no free functions
            ("ansible", d.EXCEPTIONS): False,  # nothing to be right or wrong about
        }
        for (language, question), expected in cases.items():
            with self.subTest(language=language, question=question):
                self.assertEqual(expected, d.model_justified(language, question))

    def test_purpose_is_the_only_field_the_prompt_still_has_a_case_for(self):
        """The epic's claim, as an assertion rather than a sentence in a manifest."""
        justified = {q for q in d.PROMPTED if d.model_justified("python", q)}

        self.assertEqual({d.PURPOSE}, justified)


class TestTheExceptionsArgument(unittest.TestCase):
    """What ``methods[].exceptions`` in ``data_from_code.yaml`` rests on.

    The field looks decidable because in one well-known language half of it
    is. Pinning both halves is what stops the question being reopened from
    memory next time.
    """

    def test_python_cannot_settle_the_exceptions_a_method_throws(self):
        entry = d.decide("python", d.EXCEPTIONS)

        self.assertEqual(d.NOT_IN_FILE, entry.verdict)
        self.assertEqual("", entry.settled_by)
        self.assertIn("transitive", entry.reason)

    def test_java_settles_the_declared_half_and_not_the_other(self):
        entry = d.decide("java", d.EXCEPTIONS)

        self.assertEqual(d.PARTIAL, entry.verdict)
        self.assertIn("throws", entry.settled_by)
        self.assertIn("unchecked", entry.reason.lower())

    def test_the_two_languages_disagree_which_is_why_the_field_is_per_language(self):
        """A single verdict for the field would be wrong in one of them.

        Both resolve against asking, and not for the same reason: Python has
        nothing to parse and nothing to read, Java has something to parse that
        nothing here parses yet.
        """
        python = d.decide("python", d.EXCEPTIONS)
        java = d.decide("java", d.EXCEPTIONS)

        self.assertNotEqual(python.verdict, java.verdict)
        self.assertFalse(d.model_justified("python", d.EXCEPTIONS))
        self.assertFalse(d.model_justified("java", d.EXCEPTIONS))

    def test_no_language_in_the_table_justifies_asking_for_exceptions(self):
        """The whole argument for deleting the field, in one assertion."""
        asking = [lang for lang in d.LANGUAGES if d.model_justified(lang, d.EXCEPTIONS)]

        self.assertEqual([], asking)

    def test_a_language_without_methods_has_no_exceptions_question_at_all(self):
        entry = d.decide("ansible", d.EXCEPTIONS)

        self.assertEqual(d.NOT_APPLICABLE, entry.verdict)
        self.assertFalse(d.model_justified("ansible", d.EXCEPTIONS))


class TestTheFieldsAreGoneFromBothPlacesTheyLived(unittest.TestCase):
    """The table above is the argument; these are the edits it licensed.

    Two artifacts, because deleting the prompt line alone leaves a field
    reachable by a second route. Phase 4 did this for
    ``methods[].exceptions`` and Phase 5 did it for the rest of the object,
    so what used to be an assertion about one field is now an assertion that
    the object itself is gone.
    """

    #: A ``methods[]`` field in the prompt's schema block: an indented
    #: ``**name**:`` with no leading number. The numbered entries are
    #: top-level fields and deliberately do not match.
    METHOD_FIELD = re.compile(r"^\s+\*\*(?P<name>[^*]+)\*\*:", re.MULTILINE)

    #: A numbered top-level field, which is the form the one survivor takes.
    TOP_LEVEL_FIELD = re.compile(r"^\s*\d+\.\s+\*\*(?P<name>[^*]+)\*\*", re.MULTILINE)

    def schema_block(self):
        return PROMPT.read_text(encoding="utf-8").split("- role: user")[0]

    def asked_per_method(self):
        return [match.group("name") for match in self.METHOD_FIELD.finditer(self.schema_block())]

    def test_the_prompt_asks_for_nothing_per_method_at_all(self):
        """Phase 4 asserted ``exceptions`` was absent from this list. It is empty now.

        That makes the narrower assertion vacuous, so the replacement is the
        stronger one it became: no per-method field survives, which covers
        ``exceptions`` along with ``code``, ``callers`` and the other three.
        """
        self.assertEqual([], self.asked_per_method())

    def test_the_only_thing_the_prompt_asks_for_is_purpose(self):
        asked = [match.group("name") for match in self.TOP_LEVEL_FIELD.finditer(self.schema_block())]

        self.assertEqual([d.PURPOSE], asked)

    def test_the_metadata_schema_no_longer_carries_the_methods_template(self):
        """``code_metadata_schema.json`` is the second route.

        ``utils.json_utils._apply_defaults_to_json_keys`` walks the schema's
        top level only, so it never backfilled per method. It did plant the
        whole ``methods[]`` template whenever a model returned no ``methods``
        key, and that template is written to ``*_metadata.txt``, copied into
        GraphRAG's ``input/`` by ``indexing.py`` and indexed. Six fields
        nothing asks for should not arrive by that path.

        The key itself stays, as an empty list. ``provenance.symbols`` fills
        it for a language it reads and the renderer wants the same shape
        either way; what goes is the object that mirrored the deleted prompt.
        """
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertEqual([], schema["methods"])
        for key in ("classes", "imports"):
            with self.subTest(key=key):
                self.assertEqual([], schema[key], "the sibling list keys set the shape")

    def test_what_the_prompt_still_asks_is_what_the_table_says_it_asks(self):
        """Pins the drift in both directions, which is the point of PROMPTED."""
        asked = {name.replace(" ", "_") for name in self.asked_per_method()}
        asked |= {
            match.group("name").replace(" ", "_")
            for match in self.TOP_LEVEL_FIELD.finditer(self.schema_block())
        }

        self.assertEqual(set(d.PROMPTED), asked)

    def test_every_field_the_prompt_dropped_still_has_a_verdict_against_it(self):
        """Deleting a field must not delete the argument that deleted it.

        Six questions left the prompt in Phase 5 and one in Phase 4. All seven
        stay in ``QUESTIONS``, and ``model_justified`` still refuses each of
        them for Python, so re-adding one has to argue past a verdict.
        """
        dropped = (
            d.METHOD_CODE, d.CALLERS, d.PARAMETERS, d.TEMPLATE_PARAMETERS,
            d.ANNOTATIONS, d.RETURN_VALUE, d.EXCEPTIONS,
        )

        for question in dropped:
            with self.subTest(question=question):
                self.assertIn(question, d.QUESTIONS)
                self.assertNotIn(question, d.PROMPTED)
                self.assertFalse(d.model_justified("python", question))


if __name__ == "__main__":
    unittest.main()
