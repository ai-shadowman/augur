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
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def graphrag_indexing_op(codebase_dir: Input[Dataset],
                          graphrag_dir: Output[Dataset], result: Output[Metrics],
                          git_slug: str = None, multi_repo: bool = False):

    from pipelines.base.indexing import run_full_pipeline as _run_full_pipeline
    from utils.kubeflow_utils import read_from_input_artifact, write_to_output_artifact

    with read_from_input_artifact(codebase_dir) as tmp_codebase, \
         write_to_output_artifact(graphrag_dir) as tmp_graphrag:

        pipeline_result = _run_full_pipeline(
            codebase_path=tmp_codebase,
            graphrag_source_path=tmp_graphrag,
            git_slug=git_slug,
            multi_repo=multi_repo,
        )

        result.log_metric("success", 1 if pipeline_result.get("status") == "success" else 0)

        for key, value in pipeline_result.items():

            if isinstance(value, (int, float)):

                result.log_metric(key, value)

        if pipeline_result.get("status") != "success":

            raise RuntimeError(f"GraphRAG indexing failed: {pipeline_result.get('fail_message')}")


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="graphrag-indexing-pipeline")
def run_full_pipeline(
    codebase_dir: Input[Dataset],
    git_slug: str = None,
    multi_repo: bool = False,
) -> Dataset:

    task = graphrag_indexing_op(
        codebase_dir=codebase_dir,
        git_slug=git_slug,
        multi_repo=multi_repo,
    )

    return task.outputs["graphrag_dir"]
