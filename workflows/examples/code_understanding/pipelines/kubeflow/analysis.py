import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Output, Artifact
from utils.pipeline_utils import ANALYSIS_BASE_IMAGE, inject_git_creds


##############################################################################
# Components
##############################################################################

@inject_git_creds(secret_name="git-credentials", username_key="GIT_USERNAME", password_key="GIT_TOKEN")
@dsl.component(base_image=ANALYSIS_BASE_IMAGE)
def generate_migration_report_op(graphrag_source_path: str, report: Output[Artifact]):

    from pipelines.base.analysis import run_full_pipeline

    migration_report = run_full_pipeline(graphrag_source_path)

    with open(report.path, "w") as f:

        f.write(migration_report)


##############################################################################
# Pipelines
##############################################################################

@dsl.pipeline(name="graphrag-analysis-pipeline")
def run_full_pipeline(
    graphrag_source_path: str = "graph_rag_app/source",
):

    generate_migration_report_op(graphrag_source_path=graphrag_source_path)