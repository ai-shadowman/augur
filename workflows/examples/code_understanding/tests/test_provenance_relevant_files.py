"""The relevant_files validator: extraction, the three verdicts, evidence sizing.

pandas is imported inside the one test that writes a frame; see
tests/conftest.py.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from provenance import cli
from provenance import relevant_files as rf
from provenance.corpus import Corpus, CorpusFile

REPO = "https://github.com/celery/celery"
INDEXED = ("celery/app/base.py", "celery/app/__init__.py", "celery/worker/state.py")
UNINDEXED = ("celery/tests/test_app.py", "setup.py", ".git/HEAD")


def header(path, multi="false"):
    return (
        f'"""This file is located at {path} from repository url {REPO}, '
        f"repository slug celery-v2-5-5, multi_repo {multi}, runtime Python 2.7\n"
    )


def corpus(paths=INDEXED, multi=False):
    return Corpus(files=[CorpusFile(p, REPO, "celery-v2-5-5", multi) for p in paths])


def plan(*files_per_row, key="code_migration_plan"):
    rows = [{"component": f"c{i}", "relevant_files": f} for i, f in enumerate(files_per_row)]
    return json.dumps({key: rows})


class CheckoutTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.checkout = self.root / "repo"
        for relative in INDEXED + UNINDEXED:
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        self.evidence = rf.Evidence.from_checkout(corpus(), self.checkout)


class TestExtract(unittest.TestCase):
    BODY = plan(["celery/app/base.py"])

    def test_forms(self):
        cases = {
            "post-amble shape": (f"### Title\n\n```json\n{self.BODY}\n```", rf.Form.FENCED),
            "unlabelled fence": (f"```\n{self.BODY}\n```", rf.Form.FENCED),
            "first fence is prose": (f"```\nnot json\n```\n```json\n{self.BODY}\n```", rf.Form.FENCED),
            "no fence, title above": (f"### Title\n\n{self.BODY}", rf.Form.BARE),
            "fence unclosed": (f"```json\n{self.BODY}", rf.Form.BARE),
            "truncated": (f"```json\n{self.BODY[:-5]}", rf.Form.MALFORMED),
            "prose only": ("I could not produce a plan.", rf.Form.MALFORMED),
            "empty": ("", rf.Form.MALFORMED),
        }
        for name, (answer, form) in cases.items():
            with self.subTest(name):
                parsed, got = rf.extract(answer)
                self.assertIs(got, form, answer)
                if form is not rf.Form.MALFORMED:
                    self.assertEqual(json.loads(self.BODY), parsed)


class TestResolve(CheckoutTestCase):
    def test_verdicts(self):
        cases = [
            ("celery/app/base.py", rf.Verdict.CONFIRMED, ("celery/app/base.py",), False),
            ("./celery/app/base.py", rf.Verdict.CONFIRMED, ("celery/app/base.py",), False),
            ("`/celery/app/base.py`", rf.Verdict.CONFIRMED, ("celery/app/base.py",), False),
            ("celery.app.base", rf.Verdict.CONFIRMED, ("celery/app/base.py",), True),
            ("CELERY/WORKER/STATE", rf.Verdict.CONFIRMED, ("celery/worker/state.py",), True),
            ("celery/app/__init__.py", rf.Verdict.CONFIRMED, ("celery/app/__init__.py",), False),
            ("setup.py", rf.Verdict.NOT_INDEXED, ("setup.py",), False),
            ("celery.tests.test_app", rf.Verdict.NOT_INDEXED, ("celery/tests/test_app.py",), True),
            # A directory is not a file path, and celery/app folds to celery.app,
            # which no file key equals: __init__ keeps its segment.
            ("celery/app", rf.Verdict.MATCHES_NOTHING, (), False),
            ("celery/app/routes.py", rf.Verdict.MATCHES_NOTHING, (), False),
            (".git/HEAD", rf.Verdict.MATCHES_NOTHING, (), False),
            ("", rf.Verdict.MATCHES_NOTHING, (), False),
            ({"path": "celery/app/base.py"}, rf.Verdict.MATCHES_NOTHING, (), False),
        ]
        for entry, verdict, resolved, folded in cases:
            with self.subTest(entry=entry):
                got = self.evidence.resolve(entry)
                self.assertEqual((verdict, resolved, folded), (got.verdict, got.resolved, got.folded))


class TestShape(CheckoutTestCase):
    """Entries taken from the smoke run's RHEL plan, 2026-10-24."""

    def test_shapes(self):
        cases = [
            ("/etc/fstab", rf.Shape.HOST_PATH),
            ("/etc/NetworkManager/dispatcher.d/", rf.Shape.HOST_PATH),
            ("~/.celeryconfig", rf.Shape.HOST_PATH),
            ("/etc/sysconfig/network-scripts/ifcfg-*", rf.Shape.GLOB),
            ("src/**/*.py", rf.Shape.GLOB),
            ("deployment/**/*.war", rf.Shape.GLOB),
            ("pom.xml", rf.Shape.REPO_PATH),
            ("celery/contrib/django.py", rf.Shape.REPO_PATH),
            ("`requirements.txt`", rf.Shape.REPO_PATH),
            ("https://github.com/celery/celery/blob/v2.5.5/setup.py", rf.Shape.URL),
            ("", rf.Shape.NOT_A_PATH),
            ("  ", rf.Shape.NOT_A_PATH),
            (None, rf.Shape.NOT_A_PATH),
            ({"path": "x.py"}, rf.Shape.NOT_A_PATH),
        ]
        for entry, shape in cases:
            with self.subTest(entry=entry):
                got = self.evidence.resolve(entry)
                self.assertEqual((rf.Verdict.MATCHES_NOTHING, shape), (got.verdict, got.shape))

    def test_only_unmatched_entries_get_a_shape(self):
        for entry in ("celery/app/base.py", "setup.py", "/celery/app/base.py"):
            with self.subTest(entry=entry):
                self.assertIsNone(self.evidence.resolve(entry).shape)

    def test_render_and_totals_break_down_the_bucket(self):
        result = rf.check("2.md", plan(["/etc/fstab", "src/**/*.py", "pom.xml", "nope.py"]), self.evidence)
        self.assertIn("[1 glob, 1 host-path, 2 repo-path]", rf.render([result], self.evidence))
        self.assertIn("  matches-nothing /etc/fstab (host-path)", rf.render([result], self.evidence))
        self.assertEqual(2, rf.totals([result])[rf.Shape.REPO_PATH])


class TestCheck(CheckoutTestCase):
    def test_counts_every_entry_and_row(self):
        answer = "```json\n" + plan(
            ["celery/app/base.py", "setup.py"],
            [],
            ["celery/invented.py"],
            "celery/app/base.py",
            key="rhel_enhanced_code_migration_plan",
        ) + "\n```"
        result = rf.check("2.md", answer, self.evidence)
        self.assertEqual((rf.Form.FENCED, 4, 1), (result.form, result.rows, result.off_schema_rows))
        self.assertEqual(
            {rf.Verdict.CONFIRMED: 1, rf.Verdict.NOT_INDEXED: 1, rf.Verdict.MATCHES_NOTHING: 1},
            dict(result.counts()),
        )
        self.assertFalse(rf.clean([result]))

    def test_malformed_answer_is_counted_not_skipped(self):
        results = [
            rf.check("ok", plan(["celery/app/base.py"]), self.evidence),
            rf.check("cut", "```json\n{\"code_migration_plan\": [{\"relev", self.evidence),
        ]
        self.assertEqual(rf.Form.MALFORMED, results[1].form)
        self.assertEqual(1, rf.totals(results)[rf.Form.MALFORMED])
        self.assertFalse(rf.clean(results))
        self.assertTrue(rf.clean(results[:1]))

    def test_render_lists_everything_but_exact_confirmations(self):
        result = rf.check("1.md", plan(["celery/app/base.py", "celery.app.base", "nope.py"]), self.evidence)
        text = rf.render([result], self.evidence)
        self.assertIn("3 indexed of 5 checkout files", text)
        self.assertIn("2 confirmed, 0 not-indexed, 1 matches-nothing (1 by folded spelling)", text)
        self.assertIn("celery.app.base -> celery/app/base.py", text)
        self.assertNotIn("  confirmed       celery/app/base.py", text)


class TestEvidence(CheckoutTestCase):
    def test_git_directory_is_not_evidence(self):
        self.assertNotIn(".git/HEAD", self.evidence.checkout)
        self.assertEqual(5, len(self.evidence.checkout))

    def test_refuses_a_checkout_missing_indexed_files(self):
        with self.assertRaisesRegex(ValueError, "1 of 4 indexed paths.*celery/gone.py"):
            rf.Evidence.from_checkout(corpus((*INDEXED, "celery/gone.py")), self.checkout)

    def test_refuses_multi_repo(self):
        with self.assertRaisesRegex(ValueError, "multi-repo"):
            rf.Evidence.from_checkout(corpus(multi=True), self.checkout)

    def test_refuses_a_missing_checkout(self):
        with self.assertRaises(FileNotFoundError):
            rf.Evidence.from_checkout(corpus(), self.root / "absent")


class TestCli(CheckoutTestCase):
    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            status = cli.main(list(argv))
        return status, buffer.getvalue()

    def write_documents(self):
        import pandas as pd

        output = self.root / "output"
        output.mkdir()
        pd.DataFrame(
            {"title": list(INDEXED), "text": [header(p) + "body\n" for p in INDEXED]}
        ).to_parquet(output / "documents.parquet")
        return output

    def write_answer(self, name, text):
        path = self.root / name
        path.write_text(text)
        return str(path)

    def test_exit_status_tracks_the_verdicts(self):
        output = self.write_documents()
        good = self.write_answer("1.md", "```json\n" + plan(["celery/app/base.py"]) + "\n```")
        bad = self.write_answer("2.md", plan(["celery/app/base.py", "setup.py"]))
        status, _ = self.run_cli("relevant-files", str(output), str(self.checkout), good)
        self.assertEqual(cli.EXIT_OK, status)
        status, text = self.run_cli("relevant-files", str(output), str(self.checkout), good, bad, "--json")
        self.assertEqual(cli.EXIT_FINDINGS, status)
        payload = json.loads(text)
        self.assertEqual((3, 5), (payload["indexed"], payload["checkout"]))
        self.assertEqual(["fenced", "bare"], [a["form"] for a in payload["answers"]])

    def test_wrong_checkout_is_an_error(self):
        output = self.write_documents()
        answer = self.write_answer("1.md", plan([]))
        empty = self.root / "empty"
        empty.mkdir()
        status, _ = self.run_cli("relevant-files", str(output), str(empty), answer)
        self.assertEqual(cli.EXIT_ERROR, status)

    def test_missing_documents_table_is_an_error(self):
        answer = self.write_answer("1.md", plan([]))
        status, _ = self.run_cli("relevant-files", str(self.root), str(self.checkout), answer)
        self.assertEqual(cli.EXIT_ERROR, status)


if __name__ == "__main__":
    unittest.main()
