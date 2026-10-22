import csv
import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch


BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pipelines.base.data_generation import get_parsed_code_metadata


class MetadataChatHarness(unittest.TestCase):
    """Stubs enough of sdg_hub to run a flow end to end with no endpoint.

    The ``Flow`` stub below reads the LLMChatBlock's config out of the real
    ``flow.yaml`` and lets ``set_model_config`` write over it, which is the
    precedence the library has, so what reaches ``chat_completion`` is what
    would reach the model.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(chdir(self.root))

        # Scope replacements so the container-dependency stubs in other tests
        # cannot mask parameter errors or contaminate subsequent tests.
        modules = self.enterContext(patch.dict(sys.modules))
        for name in list(modules):
            if name == "yaml" or name.startswith("yaml."):
                del modules[name]
        yaml = importlib.import_module("yaml")

        class DataFrame:
            def __init__(self, records):
                self.records = records

            def to_csv(self, filename):
                with open(filename, "w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=self.records[0])
                    writer.writeheader()
                    writer.writerows(self.records)

        #: Every kwarg set that reached the chat client, in call order. The
        #: flow.yaml value and the set_model_config value land in the same
        #: dict, so this is where "which one governs" is answerable.
        self.reached_client = []

        def chat_completion(**kwargs):
            self.reached_client.append(dict(kwargs))
            # Reproduce the SDK rejection observed in the production logs.
            if "best_of" in kwargs:
                raise TypeError(
                    "AsyncCompletions.create() got an unexpected keyword argument 'best_of'"
                )
            return '{"purpose": "Manage train schedules", "classes": ["Train"]}'

        class Flow:
            @classmethod
            def from_yaml(cls, filename):
                flow = cls()
                blocks = yaml.safe_load(Path(filename).read_text())["blocks"]
                flow.parameters = next(
                    dict(block["block_config"]) for block in blocks
                    if block["block_type"] == "LLMChatBlock"
                )
                for key in ("block_name", "input_cols", "output_cols", "async_mode"):
                    flow.parameters.pop(key, None)
                return flow

            def set_model_config(self, **kwargs):
                self.parameters.update(kwargs)

            def generate(self, dataset, **kwargs):
                metadata = chat_completion(**self.parameters)
                output = DataFrame([
                    {**row, "extracted_data": metadata} for row in dataset.records
                ])
                return types.SimpleNamespace(to_pandas=lambda: output)

        class AssetLoader:
            def download_dir(self, asset_dir, download_dir):
                shutil.copytree(BASE_DIR / "assets" / asset_dir, download_dir)

        modules.update({
            "datasets": types.SimpleNamespace(Dataset=types.SimpleNamespace(
                from_pandas=lambda frame: frame,
            )),
            "sdg_hub.core.flow": types.SimpleNamespace(Flow=Flow),
            "flows.flow_extensions": types.SimpleNamespace(CustomDeleteColumnsBlock=object),
            "loaders.default_asset_loader": types.SimpleNamespace(DefaultAssetLoader=AssetLoader),
            "utils.token_tracker": types.SimpleNamespace(TokenCostTracker=types.SimpleNamespace(
                get_instance=lambda: None,
            )),
        })
        self.enterContext(patch.dict(os.environ, {
            "GRAPHRAG_LLM_PROVIDER": "openrouter",
            "GRAPHRAG_LLM_ID": "openai/gpt-oss-120b",
            "GRAPHRAG_LLM_API_BASE": "https://openrouter.ai/api/v1/",
            "GRAPHRAG_LLM_TOKEN": "test-token",
        }))
        self.frame = DataFrame([{"code": "class Train {}", "file_path": "Train.java"}])

    def assert_metadata_extracted(self, config):
        result = get_parsed_code_metadata(self.frame, language="java", config=config)

        self.assertEqual(result.records[0]["code"], "class Train {}")
        self.assertEqual(json.loads(result.records[0]["extracted_data"]), {
            "purpose": "Manage train schedules", "classes": ["Train"],
        })
        self.assertEqual(len(list(self.root.glob("data_java_*.csv"))), 1)


class TestMetadataChatParameters(MetadataChatHarness):
    def test_java_source_metadata_reaches_chat_client_without_unsupported_parameter(self):
        self.assert_metadata_extracted(config=False)

    def test_java_config_metadata_reaches_chat_client_without_unsupported_parameter(self):
        self.assert_metadata_extracted(config=True)


class TestOneNumberGovernsTheOutputBudget(MetadataChatHarness):
    """``max_tokens`` was written in three places and read from one.

    Both ``flow.yaml`` files said 64,000 and neither took effect, because
    ``get_parsed_code_metadata`` passed ``max_tokens=32_000`` to
    ``set_model_config``, and sdg_hub's ``apply_config_to_blocks`` setattrs
    straight onto the LLM block. A number that is written next to the prompt
    and silently discarded is worse than no number: the next person tunes the
    one they can see. The code override is gone, so each flow's YAML now owns
    its own budget, and these tests read what reached the client rather than
    what any one file says.

    The reduction is measured against what ran, not against what was written:
    32,000 effective to 1,024 is 31.25x. Against the dead 64,000 it would read
    62.5x, which would be claiming credit for a number nothing ever sent.
    """

    #: Effective before Phase 5, for both flows, from the code override.
    WAS = 32_000

    def budget_for(self, config):
        self.assert_metadata_extracted(config=config)
        self.assertEqual(1, len(self.reached_client), "expected exactly one chat call")
        return self.reached_client[0]["max_tokens"]

    def test_the_code_no_longer_overrides_what_the_flow_declares(self):
        source = (BASE_DIR / "pipelines" / "base" / "data_generation.py").read_text(
            encoding="utf-8"
        )
        call = source.split("flow.set_model_config(")[1].split(")")[0]

        self.assertNotIn("max_tokens", call, "the override is back; the YAMLs are dead again")

    def test_the_one_field_prompt_gets_a_summary_sized_budget(self):
        budget = self.budget_for(config=False)

        self.assertEqual(1_024, budget)
        self.assertEqual(31.25, self.WAS / budget)

    def test_the_config_flow_keeps_what_it_actually_ran_with(self):
        """Removing the override must not silently double a budget.

        ``config_generation``'s YAML said 64,000 and it ran at 32,000. Left
        alone it would have jumped to 64,000 on this commit, which is a
        behaviour change nobody asked for and no measurement supports: that
        prompt still asks for a list of every library in the file, and
        nothing here has measured how long that gets.
        """
        self.assertEqual(self.WAS, self.budget_for(config=True))

    def test_the_two_flows_are_free_to_disagree_which_is_the_point(self):
        for config in (False, True):
            get_parsed_code_metadata(self.frame, language="java", config=config)
        code, configuration = (call["max_tokens"] for call in self.reached_client)

        self.assertLess(code, configuration)


if __name__ == "__main__":
    unittest.main()
