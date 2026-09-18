import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Metrics, Output
from utils.kubeflow_utils import get_pip_installable_git_url, INDEXING_BASE_IMAGE, inject_secret_as_env

_AGENTMESH_INSTALLABLE_URL = get_pip_installable_git_url(
    git_username=os.getenv("AUGUR_GIT_REPO_USERNAME"),
    git_token=os.getenv("AUGUR_GIT_REPO_TOKEN"),
    repo_url=os.getenv("AUGUR_GIT_REPO_URL", ""),
    repo_ref=os.getenv("AUGUR_GIT_REPO_BRANCH", "main"),
    subdirectory="workflows/examples/code_understanding",
)


##############################################################################
# Components
##############################################################################

@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def graphrag_indexing_op(codebase_dir: Input[Dataset],
                          graphrag_dir: Output[Dataset], result: Output[Metrics],
                          git_repo: str = "", git_branch: str = "", multi_repo: bool = False):

    import logging
    import os
    from pipelines.base.indexing import generate_graphrag_index
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    setup_logging()

    git_repo = git_repo or os.getenv("GIT_REPO", "")
    git_branch = git_branch or os.getenv("GIT_BRANCH", "main")

    with read_from_input_artifact(codebase_dir) as tmp_codebase, \
         write_to_output_artifact(graphrag_dir) as tmp_graphrag:

        try:
            generate_graphrag_index(
                codebase_path=tmp_codebase,
                graphrag_source_path=tmp_graphrag,
                git_repo=git_repo,
                git_branch=git_branch,
                multi_repo=multi_repo,
            )
            result.log_metric("success", 1)
        finally:
            for save_dir in [tmp_graphrag, os.path.join(tmp_graphrag, "output")]:
                try:
                    os.makedirs(save_dir, exist_ok=True)
                    from utils.duration_tracker import DurationTracker
                    dur_tr = DurationTracker.get_instance()
                    dur_tr.save_to_file(os.path.join(save_dir, "durations.json"))
                except Exception as e:
                    logging.debug(f"Failed to persist durations.json to {save_dir}: {e}")

                try:
                    from utils.token_tracker import TokenCostTracker
                    tok_tr = TokenCostTracker.get_instance()
                    tok_tr.save_to_file(os.path.join(save_dir, "tokens.json"))
                except Exception as e:
                    logging.debug(f"Failed to persist tokens.json to {save_dir}: {e}")

    try:
        from utils.duration_tracker import DurationTracker
        dur_tr = DurationTracker.get_instance()
        summary = dur_tr.format_summary()
        logging.info("\n" + summary)
        print("\n" + summary, flush=True)
    except Exception as e:
        logging.debug(f"Failed to print duration summary in indexing pod: {e}")

    try:
        from utils.token_tracker import TokenCostTracker
        tok_tr = TokenCostTracker.get_instance()
        summary = tok_tr.format_summary()
        logging.info("\n" + summary)
        print("\n" + summary, flush=True)
    except Exception as e:
        logging.debug(f"Failed to print token summary in indexing pod: {e}")


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def graphrag_evaluation_op(graphrag_dir: Input[Dataset], eval_results: Output[Dataset],
                            git_repo: str = "", git_branch: str = "",
                            multi_repo: bool = False):

    import logging
    import os
    import pandas as pd
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact
    setup_logging()

    git_repo = git_repo or os.getenv("GIT_REPO", "")
    git_branch = git_branch or os.getenv("GIT_BRANCH", "main")

    if not git_repo or multi_repo:
        logging.info("Skipping evaluation: git_repo not provided or multi_repo=True.")
        pd.DataFrame().to_csv(eval_results.path, index=False)
        return

    from pipelines.base.indexing import evaluate_graphrag_index

    with read_from_input_artifact(graphrag_dir) as tmp_graphrag:

        results = evaluate_graphrag_index(
            graphrag_source_path=tmp_graphrag,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

    df = results if isinstance(results, pd.DataFrame) else pd.DataFrame(results or [])
    df.to_csv(eval_results.path, index=False)


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def run_indexing_multi_repo_op(parent_target_path: str,
                                graphrag_dir: Output[Dataset],
                                eval_results: Output[Dataset]):
    """Runs GraphRAG indexing and evaluation across the combined multi-repo codebase."""

    import logging
    import os
    import pandas as pd
    from pipelines.base.indexing import IndexingPipeline
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact
    setup_logging()

    with write_to_output_artifact(graphrag_dir) as tmp_graphrag:
        try:
            IndexingPipeline().run_multi_repo(parent_target_path, graphrag_source_path=tmp_graphrag)
        finally:
            for save_dir in [tmp_graphrag, os.path.join(tmp_graphrag, "output")]:
                try:
                    os.makedirs(save_dir, exist_ok=True)
                    from utils.duration_tracker import DurationTracker
                    dur_tr = DurationTracker.get_instance()
                    dur_tr.save_to_file(os.path.join(save_dir, "durations.json"))
                except Exception as e:
                    logging.debug(f"Failed to persist durations.json in run_indexing_multi_repo_op: {e}")

                try:
                    from utils.token_tracker import TokenCostTracker
                    tok_tr = TokenCostTracker.get_instance()
                    tok_tr.save_to_file(os.path.join(save_dir, "tokens.json"))
                except Exception as e:
                    logging.debug(f"Failed to persist tokens.json in run_indexing_multi_repo_op: {e}")

    pd.DataFrame().to_csv(eval_results.path, index=False)


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="graphrag-indexing-pipeline")
def _run_pipeline(
    codebase_dir: Input[Dataset],
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    multi_repo: bool = False,
) -> Dataset:

    task = graphrag_indexing_op(
        codebase_dir=codebase_dir,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
    )

    graphrag_evaluation_op(
        graphrag_dir=task.outputs["graphrag_dir"],
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
    )

    return task.outputs["graphrag_dir"]


##############################################################################
# Pipeline stage
##############################################################################

class IndexingPipeline:
    run = staticmethod(_run_pipeline)
    run_multi_repo = staticmethod(run_indexing_multi_repo_op)
