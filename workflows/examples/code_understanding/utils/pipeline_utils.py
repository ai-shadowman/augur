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

    PYTHONPATH is also exported to program_path before the executor runs so
    that components that omit sys.path.insert (e.g. run_adhoc_query_op) are
    covered as well.

    Reads AGENTMESH_REPO_URL and AGENTMESH_REPO_REF from the environment; does
    nothing if AGENTMESH_REPO_URL is unset.

    Uses direct string manipulation rather than a yaml parse/dump round-trip so
    that the original YAML formatting is preserved exactly.
    """
    import re

    repo_url = os.getenv("AGENTMESH_REPO_URL", "")
    if not repo_url:
        logging.warning("  AGENTMESH_REPO_URL not set; skipping code injection for %s", yaml_path)
        return

    repo_ref = os.getenv("AGENTMESH_REPO_REF", "main")

    # Lines injected just before _KFP_RUNTIME=true, after ephemeral_component.py
    # has been written to program_path.
    clone_lines = [
        f'git clone --depth=1 --branch "{repo_ref}" "{repo_url}" /tmp/_agentmesh_src >/dev/null 2>&1 || true',
        'cp -r /tmp/_agentmesh_src/workflows/examples/code_understanding/. "$program_path/" 2>/dev/null || true',
        'PYTHONPATH="$program_path:${PYTHONPATH:-}"',
        'export PYTHONPATH',
    ]

    with open(yaml_path) as f:
        content = f.read()

    if "/tmp/_agentmesh_src" in content:
        logging.info("  Already injected agent-mesh clone into %s", yaml_path)
        return

    if "_KFP_RUNTIME=true" not in content:
        logging.warning("  No '_KFP_RUNTIME=true' found in %s; nothing to inject", yaml_path)
        return

    def _inject(match):
        # Preserve whatever indentation the line has in the YAML block literal.
        indent = match.group(1)
        snippet = "".join(indent + line + "\n" for line in clone_lines)
        return snippet + match.group(0)

    new_content = re.sub(r"^( *)_KFP_RUNTIME=true", _inject, content, flags=re.MULTILINE)

    with open(yaml_path, "w") as f:
        f.write(new_content)

    count = len(re.findall(r"^( *)_KFP_RUNTIME=true", content, re.MULTILINE))
    logging.info("  Injected agent-mesh clone into %d executor(s) in %s", count, yaml_path)


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
