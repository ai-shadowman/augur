"""Every `answers[...]` reference in a report prompt names a prompt that ran.

`generate_migration_report` stores each prompt's result under its front-matter
`key:` and later prompts read earlier results by that key. A reference that
names no key, or a key whose prompt has not run yet, renders "N/A" into the
prompt without any error, so this is the only place the mistake can surface.
"""

import importlib
import os
import re
import sys
import unittest
from unittest.mock import patch

from loaders import asset_loader
from loaders.asset_loader import AssetLoader

REPORT_DIR = os.path.join(AssetLoader._PROMPTS_DIR, "analysis", "migration-report")
ANSWER_REF = re.compile(r"answers\[([^\]]*)\]")
QUOTED_KEY = re.compile(r"""^\s*(['"])([^'"]+)\1\s*$""")


def _numbered(directory):
    """The `.txt` prompts in one directory, in the order the report runs them."""
    stems = [f[:-4] for f in os.listdir(directory) if f.endswith(".txt")]
    return [os.path.join(directory, f"{s}.txt") for s in sorted(stems, key=int)]


def report_prompts():
    """(path, meta, body) in run order: the top level, then `enhanced/`."""
    out = []
    for path in _numbered(REPORT_DIR) + _numbered(os.path.join(REPORT_DIR, "enhanced")):
        with open(path) as f:
            body, meta = AssetLoader._get_prompt_body_and_metadata(f.read())
        out.append((os.path.relpath(path, REPORT_DIR), meta, body))
    return out


class TestAnswerKeys(unittest.TestCase):

    def setUp(self):
        # Other modules stub `yaml` in sys.modules at import time, and
        # asset_loader keeps whichever one it saw first. Parse with the real one.
        modules = self.enterContext(patch.dict(sys.modules))
        for name in [n for n in modules if n == "yaml" or n.startswith("yaml.")]:
            del modules[name]
        self.enterContext(patch.object(asset_loader, "yaml", importlib.import_module("yaml")))
        self.prompts = report_prompts()

    def test_every_prompt_declares_a_unique_key(self):
        keys = [(path, meta.get("key")) for path, meta, _ in self.prompts]
        missing = [path for path, key in keys if not key]
        self.assertEqual(missing, [], "prompts with no front-matter `key:`")
        seen = {}
        for path, key in keys:
            self.assertNotIn(key, seen, f"{path} reuses key {key!r} from {seen.get(key)}")
            seen[key] = path

    def test_every_reference_names_a_prompt_that_already_ran(self):
        ran = set()
        refs = 0
        for path, meta, body in self.prompts:
            for raw in ANSWER_REF.findall(body):
                refs += 1
                quoted = QUOTED_KEY.match(raw)
                self.assertIsNotNone(
                    quoted, f"{path}: answers[{raw}] is not a quoted key; positions shift")
                key = quoted.group(2)
                self.assertIn(
                    key, ran,
                    f"{path}: answers[{raw}] names no prompt that runs before it; "
                    f"earlier keys are {sorted(ran)}")
            ran.add(meta.get("key"))
        self.assertGreater(refs, 0, "no answers references found; the regex has drifted")


if __name__ == "__main__":
    unittest.main()
