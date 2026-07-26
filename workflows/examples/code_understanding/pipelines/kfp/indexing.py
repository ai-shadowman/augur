import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Output, Artifact
from utils.pipeline_utils import INDEXING_BASE_IMAGE


##############################################################################
# Components
##############################################################################

@dsl.component(base_image=INDEXING_BASE_IMAGE)
def generate_graphrag_index_op(codebase_path: str, graphrag_source_path: str,
                                git_slug: str = "", multi_repo: bool = False):

    from pipelines.base.indexing import generate_graphrag_index

    generate_graphrag_index(codebase_path=codebase_path,
                            graphrag_source_path=graphrag_source_path,
                            git_slug=git_slug or None, multi_repo=multi_repo)


@dsl.component(base_image=INDEXING_BASE_IMAGE)
def graphrag_indexing_op(codebase_path: str, graphrag_source_path: str,
                          result: Output[Artifact]):

    import json

    from pipelines.base.indexing import run_full_pipeline as _run

    pipeline_result = _run(codebase_path=codebase_path,
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
