import functools
import os
import shutil
import tempfile
from abc import ABC, abstractmethod

_DEFAULT_EVAL_DATASET = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__),
        "..", "assets", "datasets", "eval", "code_understanding.csv",
    )
)
_DEFAULT_RO_TIKTOKEN_DIR = "/opt/app-root/src/tiktoken_cache"


def _get_token_tracker():
    """Returns the shared TokenCostTracker instance if available."""
    try:
        from utils.token_tracker import TokenCostTracker
        return TokenCostTracker.get_instance()
    except Exception:
        return None


def _ensure_writable_tiktoken_cache() -> str:
    """Ensures TIKTOKEN_CACHE_DIR points to a writable directory so gitingest/tiktoken
    does not fail with PermissionError in read-only container filesystems."""
    current_dir = os.environ.get("TIKTOKEN_CACHE_DIR") or (
        _DEFAULT_RO_TIKTOKEN_DIR if os.path.isdir(_DEFAULT_RO_TIKTOKEN_DIR) else ""
    )
    if current_dir and os.path.isdir(current_dir) and os.access(current_dir, os.W_OK):
        os.environ["TIKTOKEN_CACHE_DIR"] = current_dir
        return current_dir

    writable_dir = os.path.join(tempfile.gettempdir(), "tiktoken_cache")
    os.makedirs(writable_dir, exist_ok=True)

    if current_dir and os.path.isdir(current_dir):
        for entry in os.listdir(current_dir):
            if entry.endswith(".tmp"):
                continue
            src_path = os.path.join(current_dir, entry)
            dst_path = os.path.join(writable_dir, entry)
            if os.path.isfile(src_path) and not os.path.exists(dst_path):
                shutil.copy2(src_path, dst_path)

    os.environ["TIKTOKEN_CACHE_DIR"] = writable_dir
    return writable_dir


@functools.lru_cache(maxsize=None)
def build_repo_context(graphrag_source_dir: str = "") -> str:
    """Returns codebase source as LLM-ready context by ingesting graphrag_source_dir/input.

    Result is cached so repeated calls for the same directory do not re-run gitingest.
    Logs a warning and returns an empty string if no directory is provided, the directory
    is missing, or ingestion fails.
    """
    import pathlib, logging

    if not graphrag_source_dir:

        logging.warning("No graphrag_source_dir provided; ground truth LLM will have no source context.")

        return ""

    codebase_dir = pathlib.Path(graphrag_source_dir) / "input"

    if not codebase_dir.is_dir():

        logging.warning("Codebase directory %s not found; ground truth LLM will have no source context.", codebase_dir)

        return ""

    try:
        _ensure_writable_tiktoken_cache()
        from gitingest import ingest
        _, _, content = ingest(
            str(codebase_dir),
            max_file_size=50_000, # TODO: make this an environment variable setting
            exclude_patterns={"*.lock", "*.min.js", "*.min.css", "dist/*", "vendor/*",
                              "node_modules/*", "*.egg-info/*", "*.pyc", "__pycache__/*", "*_metadata.txt"},
        )

        _MAX_CONTEXT_CHARS = 480_000 # TODO: make this an environment variable setting
        if len(content) > _MAX_CONTEXT_CHARS:
            logging.warning("Codebase context truncated from %d to %d chars (~120K tokens).",
                            len(content), _MAX_CONTEXT_CHARS)
            content = content[:_MAX_CONTEXT_CHARS]

        return f"Source code of the codebase being analyzed:\n\n{content}"

    except Exception as e:
        logging.warning("Failed to ingest codebase from %s: %s", codebase_dir, e)

    return ""


class CustomEvaluator(ABC):

    @abstractmethod
    def evaluate(self, input: str, graphrag_source_dir: str, git_repo: str, git_branch: str,
                 git_slug: str = None, multi_repo: bool = False):
        """Evaluates a single input against a GraphRAG index using LLM-as-judge.

        Args:
            input: The question or prompt to evaluate.
            graphrag_source_dir: Root directory of the GraphRAG index.
            git_repo: Repository URL used as context for the ground truth LLM.
            git_branch: Branch name used as context for the ground truth LLM.
            git_slug: Optional repository slug used to scope results.
            multi_repo: Whether the index spans multiple repositories.

        Returns:
            dict of metric scores and metadata for the evaluated input.
        """

    @abstractmethod
    def evaluate_with_dataset(
        self,
        graphrag_source_dir: str,
        git_repo: str,
        git_branch: str,
        eval_dataset_file: str = _DEFAULT_EVAL_DATASET,
        git_slug: str = None,
        multi_repo: bool = False,
    ):
        """Runs evaluate() for every row in a CSV dataset and uploads the results.

        Args:
            graphrag_source_dir: Root directory of the GraphRAG index
                (must contain output/*.parquet files).
            git_repo: Repository URL used as context for the ground truth LLM.
            git_branch: Branch name used as context for the ground truth LLM.
            eval_dataset_file: Path to the evaluation CSV. Defaults to
                assets/datasets/eval/code_understanding.csv.
            git_slug: Optional repository slug used to scope results and artifact paths.
            multi_repo: Whether the index spans multiple repositories.

        Returns:
            The updated pandas DataFrame with "answer" and metric columns populated.
        """
