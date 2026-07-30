import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Markdown, Output
from utils.kubeflow_utils import ANALYSIS_BASE_IMAGE, get_pip_installable_git_url, inject_secret_as_env

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
@dsl.component(base_image=ANALYSIS_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_migration_report_op(graphrag_dir: Input[Dataset], report: Output[Markdown]):

    import os
    from pipelines.base.analysis import run_full_pipeline
    from utils.kubeflow_utils import read_from_input_artifact

    with read_from_input_artifact(graphrag_dir) as tmp_graphrag:

        migration_report = run_full_pipeline(tmp_graphrag)

    os.makedirs(os.path.dirname(report.path), exist_ok=True)

    with open(report.path, "w") as f:

        f.write(migration_report)


##############################################################################
# Pipelines
##############################################################################

@dsl.pipeline(name="graphrag-analysis-pipeline")
def run_full_pipeline(
    graphrag_dir: Input[Dataset],
):

    generate_migration_report_op(graphrag_dir=graphrag_dir)
