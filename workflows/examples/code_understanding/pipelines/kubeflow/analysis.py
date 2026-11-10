import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Markdown, Output
from utils.kubeflow_utils import ANALYSIS_BASE_IMAGE, get_pip_installable_git_url, inject_secret_as_env

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
@dsl.component(base_image=ANALYSIS_BASE_IMAGE, install_kfp_package=False)
def generate_migration_report_op(graphrag_dir: Input[Dataset], report: Output[Markdown],
                                  git_repo: str = "", git_branch: str = "",
                                  multi_repo: bool = False,
                                  pipeline_run_id: str = "",
                                  project_slug: str = "",
                                  project_workspace: str = ""):

    import logging
    import os
    from pipelines.base.analysis import write_migration_report
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact
    setup_logging()

    if project_workspace:
        os.environ["PROJECT_WORKSPACE"] = project_workspace

    git_repo = git_repo or os.getenv("GIT_REPO", "")
    git_branch = git_branch or os.getenv("GIT_BRANCH", "main")

    with read_from_input_artifact(graphrag_dir) as tmp_graphrag:
        write_migration_report(
            tmp_graphrag,
            report.path,
            git_repo=git_repo,
            git_branch=git_branch,
            multi_repo=multi_repo,
            pipeline_run_id=pipeline_run_id,
            project_slug=project_slug,
        )

    try:
        from utils.duration_tracker import DurationTracker
        dur_tr = DurationTracker.get_instance()
        summary = dur_tr.format_summary()
        logging.info("\n" + summary)
    except Exception as e:
        logging.debug(f"Failed to print duration summary in analysis pod: {e}")

    try:
        from utils.token_tracker import TokenCostTracker
        tok_tr = TokenCostTracker.get_instance()
        summary = tok_tr.format_summary()
        logging.info("\n" + summary)
    except Exception as e:
        logging.debug(f"Failed to print token summary in analysis pod: {e}")

    try:
        from utils.code_metrics_tracker import CodeMetricsTracker
        code_tr = CodeMetricsTracker.get_instance()
        summary = code_tr.format_summary()
        logging.info("\n" + summary)
    except Exception as e:
        logging.debug(f"Failed to print code metrics summary in analysis pod: {e}")


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=ANALYSIS_BASE_IMAGE, install_kfp_package=False)
def run_analysis_multi_repo_op(
    graphrag_dir: Input[Dataset],
    report: Output[Markdown],
    pipeline_run_id: str = "",
    project_slug: str = "",
    project_workspace: str = "",
):
    """Runs migration report generation across the combined multi-repo GraphRAG index."""

    import logging
    import os
    from pipelines.base.analysis import write_migration_report
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact
    setup_logging()

    if project_workspace:
        os.environ["PROJECT_WORKSPACE"] = project_workspace

    with read_from_input_artifact(graphrag_dir) as tmp_graphrag:
        write_migration_report(
            tmp_graphrag,
            report.path,
            multi_repo=True,
            pipeline_run_id=pipeline_run_id,
            project_slug=project_slug,
        )

    try:
        from utils.code_metrics_tracker import CodeMetricsTracker
        code_tr = CodeMetricsTracker.get_instance()
        summary = code_tr.format_summary()
        logging.info("\n" + summary)
    except Exception as e:
        logging.debug(f"Failed to print code metrics summary in analysis pod: {e}")


##############################################################################
# Pipeline
##############################################################################

@dsl.pipeline(name="graphrag-analysis-pipeline")
def _run_pipeline(
    graphrag_dir: Input[Dataset],
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    multi_repo: bool = False,
    pipeline_run_id: str = "",
    project_slug: str = "",
    project_workspace: str = "",
):

    generate_migration_report_op(
        graphrag_dir=graphrag_dir,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
        pipeline_run_id=pipeline_run_id,
        project_slug=project_slug,
        project_workspace=project_workspace,
    )


##############################################################################
# Pipeline stage
##############################################################################

class AnalysisPipeline:
    run = staticmethod(_run_pipeline)
    run_multi_repo = staticmethod(run_analysis_multi_repo_op)
