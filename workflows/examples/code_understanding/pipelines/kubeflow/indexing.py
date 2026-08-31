import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Metrics, Output
from utils.kubeflow_utils import get_pip_installable_git_url, INDEXING_BASE_IMAGE, inject_secret_as_env

_AGENTMESH_INSTALLABLE_URL = get_pip_installable_git_url(
    git_username=os.getenv("GIT_USERNAME"),
    git_token=os.getenv("GIT_TOKEN"),
    repo_url=os.getenv("AGENTMESH_REPO_URL", ""),
    repo_ref=os.getenv("AGENTMESH_REPO_REF", "main"),
    subdirectory="workflows/examples/code_understanding",
)


##############################################################################
# Components
##############################################################################

@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(
    base_image=INDEXING_BASE_IMAGE, 
    packages_to_install=[_AGENTMESH_INSTALLABLE_URL] if _AGENTMESH_INSTALLABLE_URL else None,
    pip_index_urls=[os.getenv("PYPI_MIRROR")] if os.getenv("PYPI_MIRROR") else None
)
def graphrag_indexing_op(codebase_dir: Input[Dataset],
                          graphrag_dir: Output[Dataset], result: Output[Metrics],
                          git_repo: str = "", git_branch: str = "", multi_repo: bool = False):

    from pipelines.base.indexing import generate_graphrag_index
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    setup_logging()

    with read_from_input_artifact(codebase_dir) as tmp_codebase, \
         write_to_output_artifact(graphrag_dir) as tmp_graphrag:

        generate_graphrag_index(
            codebase_path=tmp_codebase,
            graphrag_source_path=tmp_graphrag,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
        )

        result.log_metric("success", 1)


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(
    base_image=INDEXING_BASE_IMAGE, 
    packages_to_install=[_AGENTMESH_INSTALLABLE_URL] if _AGENTMESH_INSTALLABLE_URL else None,
    pip_index_urls=[os.getenv("PYPI_MIRROR")] if os.getenv("PYPI_MIRROR") else None
)
def graphrag_evaluation_op(graphrag_dir: Input[Dataset], eval_results: Output[Dataset],
                            git_repo: str = "", git_branch: str = "",
                            multi_repo: bool = False):

    import logging
    import pandas as pd
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact
    setup_logging()

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
@dsl.component(
    base_image=INDEXING_BASE_IMAGE, 
    packages_to_install=[_AGENTMESH_INSTALLABLE_URL] if _AGENTMESH_INSTALLABLE_URL else None,
    pip_index_urls=[os.getenv("PYPI_MIRROR")] if os.getenv("PYPI_MIRROR") else None
)
def run_indexing_multi_repo_op(parent_target_path: str,
                                graphrag_dir: Output[Dataset],
                                eval_results: Output[Dataset]):
    """Runs GraphRAG indexing and evaluation across the combined multi-repo codebase."""

    import pandas as pd
    from pipelines.base.indexing import IndexingPipeline
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact
    setup_logging()

    with write_to_output_artifact(graphrag_dir) as tmp_graphrag:
        IndexingPipeline().run_multi_repo(parent_target_path, graphrag_source_path=tmp_graphrag)

    pd.DataFrame().to_csv(eval_results.path, index=False)


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="graphrag-indexing-pipeline")
def _run_pipeline(
    codebase_dir: Input[Dataset],
    git_repo: str = "",
    git_branch: str = "",
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
