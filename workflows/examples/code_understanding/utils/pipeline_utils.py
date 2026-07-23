import os
import logging
logging.basicConfig(level=logging.INFO)

DATA_GENERATION_BASE_IMAGE = (
    f"{os.getenv('KFP_IMAGE_REGISTRY')}"
    f"/{os.getenv('KFP_DATA_GENERATION_BASE_IMAGE_NAME')}"
    f":{os.getenv('KFP_DATA_GENERATION_BASE_IMAGE_VERSION')}"
)

INDEXING_BASE_IMAGE = (
    f"{os.getenv('KFP_IMAGE_REGISTRY')}"
    f"/{os.getenv('KFP_INDEXING_BASE_IMAGE_NAME')}"
    f":{os.getenv('KFP_INDEXING_BASE_IMAGE_VERSION')}"
)

ANALYSIS_BASE_IMAGE = (
    f"{os.getenv('KFP_IMAGE_REGISTRY')}"
    f"/{os.getenv('KFP_ANALYSIS_BASE_IMAGE_NAME')}"
    f":{os.getenv('KFP_ANALYSIS_BASE_IMAGE_VERSION')}"
)

def compile_and_exit(pipeline_fn):
    """Compiles pipeline_fn to YAML and exits if PIPELINE_COMPILE_ONLY is set."""
    if os.getenv("PIPELINE_COMPILE_ONLY"):
        from kfp import compiler
        compiler.Compiler().compile(pipeline_fn, os.environ.get("PIPELINE_OUTPUT_YAML", "compiled_pipeline.yaml"))
        raise SystemExit(0)


def compile_all_and_exit(pipelines: dict):
    """Compiles all pipeline functions to <KFP_PIPELINE_OUTPUT_DIR>/<name>.yaml and exits."""
    if os.getenv("PIPELINE_COMPILE_ONLY"):
        from kfp import compiler
        output_dir = os.environ.get("KFP_PIPELINE_OUTPUT_DIR", "compiled_pipelines")
        os.makedirs(output_dir, exist_ok=True)
        for name, fn in pipelines.items():
            out = os.path.join(output_dir, f"{name}.yaml")
            compiler.Compiler().compile(fn, out)
            logging.info(f"  Compiled {name} -> {out}")
        raise SystemExit(0)
