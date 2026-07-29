import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Output, Artifact
from utils.pipeline_utils import get_pip_installable_git_url, INDEXING_BASE_IMAGE, inject_git_creds

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

@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=INDEXING_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def graphrag_indexing_op(codebase_path: str, graphrag_source_path: str,
                          result: Output[Artifact]):

    import json

    from pipelines.base.indexing import run_full_pipeline as _run_full_pipeline

    pipeline_result = _run_full_pipeline(codebase_path=codebase_path,
                                         graphrag_source_path=graphrag_source_path)

    with open(result.path, "w") as f:

        json.dump(pipeline_result, f)

    if pipeline_result.get("status") != "success":

        raise RuntimeError(f"GraphRAG indexing failed: {pipeline_result.get('fail_message')}")


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="graphrag-indexing-pipeline")
def run_full_pipeline(
    codebase_path: str = os.getenv("TARGET_PATH", "target"),
    graphrag_source_path: str = "graph_rag_app/source",
):

    graphrag_indexing_op(
        codebase_path=codebase_path,
        graphrag_source_path=graphrag_source_path,
    )
