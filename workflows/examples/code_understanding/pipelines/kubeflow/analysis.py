import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Markdown, Output
from utils.pipeline_utils import ANALYSIS_BASE_IMAGE, get_pip_installable_git_url, inject_git_creds

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
@dsl.component(base_image=ANALYSIS_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_migration_report_op(graphrag_source_path: str, report: Output[Markdown]):

    import os

    from pipelines.base.analysis import run_full_pipeline

    migration_report = run_full_pipeline(graphrag_source_path)

    os.makedirs(os.path.dirname(report.path), exist_ok=True)

    with open(report.path, "w") as f:

        f.write(migration_report)


##############################################################################
# Pipelines
##############################################################################

@dsl.pipeline(name="graphrag-analysis-pipeline")
def run_full_pipeline(
    graphrag_source_path: str,
):

    generate_migration_report_op(graphrag_source_path=graphrag_source_path)
