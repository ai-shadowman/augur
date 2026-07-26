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


def uses_jupyter_runtime():
    """Returns True if the current process is running inside a Jupyter notebook."""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False


def uses_kfp():
    """Returns True if KFP is installed and the process is not running in a Jupyter notebook."""
    try:
        if uses_jupyter_runtime():
            return False
        import kfp  # noqa: F401
        return True
    except ImportError:
        return False


def _inject_agentmesh_code(yaml_path: str):
    """Post-processes a compiled KFP pipeline YAML to embed the code_understanding
    source package into every executor's startup shell script.

    KFP's generated executor script always creates a temp directory (program_path)
    and writes the component function there as ephemeral_component.py.  By
    extracting a tar archive of all subdirectories of code_understanding/ into that
    same directory we make those packages importable without any changes to the
    notebook or the container images.

    The archive is created from the local source tree at compile time and embedded
    as a base64-encoded string directly in the YAML.  This avoids needing git,
    network access, or credentials inside component pods.

    PYTHONPATH is also exported to program_path before the executor runs so that
    components that omit sys.path.insert are covered as well.

    Uses direct string manipulation rather than a yaml parse/dump round-trip so
    that the original YAML formatting is preserved exactly.
    """
    import re
    import io
    import tarfile
    import base64 as _b64

    # Locate code_understanding/ from this file's location (utils/pipeline_utils.py).
    this_dir = os.path.dirname(os.path.abspath(__file__))  # .../utils/
    code_dir = os.path.dirname(this_dir)                    # .../code_understanding/

    # Collect all subdirectories and the top-level __init__.py (if present).
    entries = sorted(os.scandir(code_dir), key=lambda e: e.name)
    embed_dirs = [e.name for e in entries if e.is_dir()]
    if not embed_dirs:
        logging.warning(
            "  No subdirs found under %s; skipping injection for %s",
            code_dir, yaml_path,
        )
        return

    def _skip_pycache(info: tarfile.TarInfo):
        if "__pycache__" in info.name or info.name.endswith(".pyc"):
            return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Include top-level __init__.py if present so code_understanding is a package.
        init_py = os.path.join(code_dir, "__init__.py")
        if os.path.isfile(init_py):
            tar.add(init_py, arcname="__init__.py")
        for d in embed_dirs:
            tar.add(os.path.join(code_dir, d), arcname=d, filter=_skip_pycache)
    buf.seek(0)
    b64_data = _b64.b64encode(buf.read()).decode("ascii")

    # Lines injected just before _KFP_RUNTIME=true, after ephemeral_component.py
    # has been written to program_path.  printf is a POSIX shell builtin so there
    # is no exec argument-length limit even for very large base64 payloads.
    inject_lines = [
        f"printf '%s' '{b64_data}' | base64 -d | tar -xz -C \"$program_path/\"",
        'echo "[agentmesh-inject] program_path contents:" >&2',
        'ls "$program_path/" >&2',
        'PYTHONPATH="$program_path:${PYTHONPATH:-}"',
        'export PYTHONPATH',
    ]

    with open(yaml_path) as f:
        content = f.read()

    if "base64 -d | tar -xz" in content:
        logging.info("  Already injected embedded code into %s", yaml_path)
        return

    if "_KFP_RUNTIME=true" not in content:
        logging.warning("  No '_KFP_RUNTIME=true' found in %s; nothing to inject", yaml_path)
        return

    def _inject(match):
        # Preserve whatever indentation the line has in the YAML block literal.
        indent = match.group(1)
        snippet = "".join(indent + line + "\n" for line in inject_lines)
        return snippet + match.group(0)

    new_content = re.sub(r"^( *)_KFP_RUNTIME=true", _inject, content, flags=re.MULTILINE)

    with open(yaml_path, "w") as f:
        f.write(new_content)

    count = len(re.findall(r"^( *)_KFP_RUNTIME=true", content, re.MULTILINE))
    logging.info(
        "  Embedded %d dir(s) into %d executor(s) in %s",
        len(embed_dirs), count, yaml_path,
    )


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
