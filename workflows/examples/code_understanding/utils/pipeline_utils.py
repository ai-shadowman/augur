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



def get_pip_installable_git_url(
    git_username: str,
    git_token: str,
    repo_url: str,
    repo_ref: str,
    subdirectory: str,
) -> str:
    """Returns a pip-installable VCS URL with embedded credentials."""
    return (
        f"git+https://{git_username}:{git_token}"
        f"@{repo_url.removeprefix('https://')}"
        f"@{repo_ref}"
        f"#subdirectory={subdirectory}"
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


def inject_git_creds(secret_name: str, username_key: str, password_key: str):
    """Decorator factory that injects git credentials into every invocation of a @dsl.component."""
    import functools

    def decorator(component_fn):

        @functools.wraps(component_fn)
        def wrapper(*args, **kwargs):
            task = component_fn(*args, **kwargs)
            try:
                from kfp import kubernetes
                kubernetes.use_secret_as_env(
                    task,
                    secret_name=secret_name,
                    secret_key_to_env={"GIT_USERNAME": username_key, "GIT_TOKEN": password_key},
                )
            except ImportError:
                pass
            return task

        return wrapper

    return decorator


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
