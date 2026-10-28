# provenance: testing the edge checks

This package checks what the GraphRAG index claims against what the source
says. On `feat/relationship-layer` that covers the relationships as well as the
entities: each model-written edge gets a verdict from a parser-built oracle,
a few kinds of contradiction are dropped, the relationship type is a parsed
field rather than prose, and the migration report says how much of the graph
is checked.

There are three ways to test it, from free and quick to a full pipeline run.

## 1. The test suite (no model, about a minute)

From `workflows/examples/code_understanding/`:

```
python3.11 -m venv .venv && . .venv/bin/activate
pip install pathspec pyyaml 'tree-sitter>=0.23,<0.27' 'tree-sitter-language-pack>=1.22,<2' \
    pip-requirements-parser packaging pygments pandas pyarrow pytest
python -m pytest tests/ -q
```

Leave litellm and graphrag out of this venv; the suite stubs both. With the
real litellm installed, `tests/test_augurignore_data_generation.py` fails to
collect, because its own stub of `dotenv.load_dotenv` does not accept
the `override` argument litellm passes. That predates this branch.

Expect 984 passed, 5 skipped and 1 failed. The failure,
`test_duration_tracker::test_graphrag_evaluation_op_duration_tracking_and_persistence`,
predates this branch. The skips are tests that need measured indexes on disk
and skip cleanly without them.

## 2. The oracle on a real checkout (no model, seconds)

Any checkout works; celery 2.5.5 is the one the figures below come from.

```
git clone --branch v2.5.5 https://github.com/celery/celery.git /tmp/celery
python -m provenance extract /tmp/celery -o /tmp/celery-facts.json --no-syft
```

`extract` writes the facts file the indexing stage reads, including the edge
evidence the verdict needs. It reports how many files did not parse as
Python 3 and were read by tree-sitter instead (50 on celery 2.5.5).

To see the edges the oracle reads from source:

```python
from pathlib import Path
from provenance.edges import scan_checkout
from provenance.edges.calls import scan_calls

root = Path("/tmp/celery")
for (kind, target), n in scan_checkout(root).by_class().items():
    if n:
        print(kind, target, n)
print(scan_calls(root).by_category())
```

On celery 2.5.5 this prints IMPORTS 793 internal / 872 external / 5
unresolved, INHERITS 316 / 231 / 9, and 12,360 call sites by category. An edge
is `internal` only when its target is declared somewhere in the checkout;
anything the oracle could not settle is `unresolved` with a reason, never a
guess. CALLS is checked for Python only.

## 3. A full pipeline run (spends inference)

The branch changes the indexing images, the prompt assets and the pipeline
code, so all three have to come from the branch:

1. Rebuild the images (`make build-images` or `make oc-build-images`). The
   data-indexing and data-generation images now install `tree-sitter` and
   `tree-sitter-language-pack`; without them provenance cannot import.
2. Upload the assets and pipelines from the branch:
   `make upload-mlflow-assets upload-pipelines AUGUR_GIT_REPO_BRANCH=feat/relationship-layer`.
   The prompts are assets, so a run against old assets uses the old
   extraction prompt.
3. Run a single repository: `make run-pipelines PIPELINE_GIT_REPO=<repo url>`.
   A Python repository exercises the most checks.

What to look for afterwards:

| Where | What it shows |
|---|---|
| indexing log | `Workflow started: extract_graph (AUGUR typed)`, and, when grounding removed anything, a line `dropped: N no-edge, ...` |
| `<index root>/prompts/extract_graph.txt` | AUGUR's prompt, with `<relationship_type>` in the relationship tuple. GraphRAG's stock prompt here means the prompts were overwritten (fixed by `restore_prompts`) |
| `output/relationships.parquet` | `type` and `claims` columns on every model row |
| `output/edge_verdicts.parquet` | one row per relationship: `verdict` (confirmed, contradicted, out-of-reach, unchecked), `reason`, and `dropped` for removed rows |
| the migration report | a section titled "Provenance of the relationships", counted from that table with no model call |

Only `reversed`, `same-file` and `no-edge` contradictions of IMPORTS and
INHERITS are dropped. CALLS contradictions are reported and kept.
"Out of reach" means no parser can check that edge (USES, DEPENDS_ON, or an
endpoint the checkout does not declare). It does not mean the edge is wrong.

For reference, a celery run on gpt-oss-120b through OpenRouter cost about
$0.25 to $1.50 for indexing, depending on corpus size and which provider
served the calls. The pipeline has not been run through Kubeflow on this
branch: the cluster it was tested on has no Data Science Pipelines, so the
indexing and analysis code ran in a pod through the same entry points.

## Where the code is

| Module | Role |
|---|---|
| `edges/` | the oracle: IMPORTS and INHERITS for Python, Java, TypeScript and Ruby; CALLS for Python |
| `edge_evidence.py`, `call_evidence.py` | what the oracle knows, captured into the facts file, since indexing has no checkout |
| `edge_verdict.py`, `call_verdict.py` | the per-edge verdict and the drop |
| `typed_extract.py` | AUGUR's `extract_graph` workflow, which parses the type field |
| `report_section.py` | the report section |
