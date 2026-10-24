"""Stage one makes no model call unless asked to.

Arm A measured the last answer stage one asked a model for, ``Purpose:``, and
found the graph no better for it, so the call is off by default behind
``AUGUR_STAGE_ONE_PURPOSE``. Three things follow and are pinned here: the
switch reads as written, an unset switch means the flow is never entered, and
the one field the config flow used to ask a model for besides ``purpose``, its
libraries, now comes from the manifest parsers or not at all.
"""

import contextlib
import json
import os
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

from pipelines.base import data_generation  # noqa: E402  stubs must land first

REPO = "https://github.com/celery/celery"
SLUG = "celery-v2-5-5"


def no_asset_loader():
    """Stand in for ``loaders.default_asset_loader``, which imports mlflow.

    Patched into ``sys.modules`` per test rather than relying on another test
    module having stubbed mlflow first, so this file passes when run alone.
    It serves the real files under ``assets/``, since ``utils.code_utils``
    reads the language mappings through it at import.
    """
    def download(name, **_kwargs):
        return json.loads(Path(BASE_DIR, "assets", name).read_text(encoding="utf-8"))

    stub = types.ModuleType("loaders.default_asset_loader")
    stub.DefaultAssetLoader = MagicMock(name="DefaultAssetLoader")
    stub.DefaultAssetLoader.return_value.download.side_effect = download
    return patch.dict(sys.modules, {"loaders.default_asset_loader": stub})


class TestTheSwitch(unittest.TestCase):
    CASES = [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("1", True),
        ("true", True),
        (" TRUE ", True),
        ("yes", True),
        ("on", True),
    ]

    def test_each_value_reads_as_written(self):
        for value, expected in self.CASES:
            env = {} if value is None else {data_generation.PURPOSE_ENV: value}
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True):
                self.assertEqual(expected, data_generation.purpose_enabled())


class TestAnUnsetSwitchMeansNoCall(unittest.TestCase):
    """``generate_code_and_meta`` end to end, with the flow replaced by a tripwire."""

    def run_stage(self, env, config=False):
        frame = MagicMock(name="code_df")
        with patch.dict(os.environ, env, clear=True), \
                patch.object(data_generation, "generate_raw_dataset", return_value=frame), \
                patch.object(data_generation, "get_parsed_code_metadata",
                             return_value=MagicMock(name="with_purpose")) as flow, \
                patch.object(data_generation, "save_code_and_metadata_files") as save, \
                no_asset_loader(), \
                tempfile.TemporaryDirectory() as target:
            data_generation.generate_code_and_meta(
                git_repo=REPO, git_branch="v2.5.5", language="python",
                source_path="/checkout", target_path=target, config=config,
            )
        return frame, flow, save

    def test_no_flow_runs_and_the_raw_rows_are_saved_for_both_kinds_of_file(self):
        for config in (False, True):
            with self.subTest(config=config):
                frame, flow, save = self.run_stage({}, config=config)

                flow.assert_not_called()
                self.assertIs(frame, save.call_args.args[0])
                self.assertEqual("/checkout", save.call_args.kwargs["source_path"])

    def test_the_switch_restores_the_call(self):
        _frame, flow, save = self.run_stage({data_generation.PURPOSE_ENV: "1"})

        flow.assert_called_once()
        self.assertIs(flow.return_value, save.call_args.args[0])


class TestConfigLibrariesComeFromTheManifest(unittest.TestCase):
    """One config file through the real ``save_code_and_metadata_files``."""

    def save(self, rel_path, content, extracted=None, source_path=True, flatten=True):
        checkout = tempfile.TemporaryDirectory()
        target = tempfile.TemporaryDirectory()
        self.addCleanup(checkout.cleanup)
        self.addCleanup(target.cleanup)
        path = Path(checkout.name, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        row = {"code": content, "file_path": rel_path, "git_repo": REPO,
               "git_slug": SLUG, "language": "python", "multi_repo": False}
        if extracted is not None:
            row["extracted_data"] = json.dumps(extracted)
        df = MagicMock()
        df.iterrows.return_value = [(0, row)]

        flattened = (
            patch("utils.json_utils.flatten_code_metadata", return_value="stub: true")
            if flatten else contextlib.nullcontext()
        )
        with no_asset_loader(), flattened:
            data_generation.save_code_and_metadata_files(
                df=df, target_path=target.name, git_repo=REPO, git_slug=SLUG,
                language="python", config=True,
                source_path=checkout.name if source_path else None,
            )
        return Path(target.name, rel_path).with_suffix(".txt").read_text(encoding="utf-8")

    def test_a_requirements_file_lists_each_declared_library_with_its_constraint(self):
        written = self.save("requirements/default.txt", "kombu>=2.1.8,<2.2.0\nanyjson==0.3.1\n")

        self.assertIn("Dependencies:", written)
        self.assertRegex(written, r"- \[library\] kombu (>=2\.1\.8,<2\.2\.0|<2\.2\.0,>=2\.1\.8)")
        self.assertIn("- [library] anyjson ==0.3.1", written)

    def test_each_library_names_the_manifest_line_it_was_read_from(self):
        """What the model was asked to reconstruct, the parser already holds verbatim."""
        captured = {}

        def capture(obj, schema=None):
            captured.update(obj)
            return "stub: true"

        with patch("utils.json_utils.flatten_code_metadata", side_effect=capture):
            self.save("requirements/default.txt", "anyjson==0.3.1\n", flatten=False)

        self.assertEqual(
            "requirements/default.txt: anyjson==0.3.1",
            captured["libraries"][0]["library_version_source"],
        )

    def test_a_model_volunteered_library_list_is_overwritten_rather_than_merged(self):
        written = self.save(
            "requirements.txt", "kombu>=2.1.8\n",
            extracted={"libraries": [{"library_name": "rabbitmq", "library_version": "3"}]},
        )

        self.assertIn("[library] kombu", written)
        self.assertNotIn("rabbitmq", written)

    def test_a_file_no_parser_reads_claims_no_libraries(self):
        """Declining, not guessing: the extraction model still reads the file below."""
        cases = [
            ("pom.xml", "<project><dependencies/></project>\n"),
            ("setup.cfg", "[metadata]\nname = celery\n"),
        ]
        for rel_path, content in cases:
            with self.subTest(path=rel_path):
                self.assertNotIn("[library]", self.save(rel_path, content))

    def test_without_a_checkout_root_nothing_is_claimed(self):
        written = self.save("requirements.txt", "kombu>=2.1.8\n", source_path=False)

        self.assertNotIn("[library]", written)


if __name__ == "__main__":
    unittest.main()
