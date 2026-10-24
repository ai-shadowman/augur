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


class TestMetadataChatParameters(unittest.TestCase):
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

        def chat_completion(**kwargs):
            # Reproduce the SDK rejection observed in the production logs.
            for unsupported in ("best_of", "top_k"):
                if unsupported in kwargs:
                    raise TypeError(
                        f"AsyncCompletions.create() got an unexpected keyword argument '{unsupported}'"
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

    def test_java_source_metadata_reaches_chat_client_without_unsupported_parameter(self):
        self.assert_metadata_extracted(config=False)

    def test_java_config_metadata_reaches_chat_client_without_unsupported_parameter(self):
        self.assert_metadata_extracted(config=True)


if __name__ == "__main__":
    unittest.main()
