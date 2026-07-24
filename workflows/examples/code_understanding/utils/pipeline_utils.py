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


def _inject_agentmesh_code(yaml_path: str):
    """Post-processes a compiled KFP pipeline YAML to inject an agent-mesh repo
    clone into every executor's startup shell script.

    KFP's generated executor script always creates a temp directory
    (program_path) and writes the component function there as
    ephemeral_component.py. By cloning the agent-mesh repo and copying the
    code_understanding package contents into that same directory we make
    `pipelines.*` and `utils.*` importable without any changes to the notebook
    or the container images.

    PYTHONPATH is also set to program_path before the executor runs so that
    components that omit sys.path.insert (e.g. run_adhoc_query_op) are covered
    as well.

    Reads AGENTMESH_REPO_URL and AGENTMESH_REPO_REF from the environment; does
    nothing if AGENTMESH_REPO_URL is unset.
    """
    repo_url = os.getenv("AGENTMESH_REPO_URL", "")
    if not repo_url:
        return

    repo_ref = os.getenv("AGENTMESH_REPO_REF", "main")

    # These lines are injected just before _KFP_RUNTIME=true so they run after
    # ephemeral_component.py has been written to program_path.
    clone_snippet = (
        f'git clone --depth=1 --branch "{repo_ref}" "{repo_url}"'
        ' /tmp/_agentmesh_src >/dev/null 2>&1 || true\n'
        'cp -r /tmp/_agentmesh_src/workflows/examples/code_understanding/.'
        ' "$program_path/" 2>/dev/null || true\n'
        'PYTHONPATH="$program_path:${PYTHONPATH:-}"\n'
        'export PYTHONPATH\n'
    )

    import yaml

    with open(yaml_path) as f:
        pipeline = yaml.safe_load(f)

    modified = False
    executors = (pipeline.get("deploymentSpec") or {}).get("executors") or {}
    for executor in executors.values():
        container = executor.get("container")
        if not container:
            continue
        command = container.get("command")
        if not command:
            continue
        for i, part in enumerate(command):
            if (
                isinstance(part, str)
                and "_KFP_RUNTIME=true" in part
                and clone_snippet not in part
            ):
                command[i] = part.replace(
                    "_KFP_RUNTIME=true",
                    clone_snippet + "_KFP_RUNTIME=true",
                )
                modified = True
                break

    if modified:
        with open(yaml_path, "w") as f:
            yaml.dump(pipeline, f, default_flow_style=False)
        logging.info(f"  Injected agent-mesh code path into {yaml_path}")


def compile_and_exit(pipeline_fn):
    """Compiles pipeline_fn to YAML and exits if PIPELINE_COMPILE_ONLY is set."""
    if os.getenv("PIPELINE_COMPILE_ONLY"):
        from kfp import compiler
        out = os.environ.get("PIPELINE_OUTPUT_YAML", "compiled_pipeline.yaml")
        compiler.Compiler().compile(pipeline_fn, out)
        _inject_agentmesh_code(out)
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
            _inject_agentmesh_code(out)
            logging.info(f"  Compiled {name} -> {out}")
        raise SystemExit(0)
