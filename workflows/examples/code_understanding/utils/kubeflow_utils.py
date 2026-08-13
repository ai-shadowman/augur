import os
import logging
from contextlib import contextmanager

import os

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

##############################################################################
# Base images
##############################################################################

DATA_GENERATION_BASE_IMAGE = (
    f"{os.getenv('KFP_IMAGE_REGISTRY')}"
    f"/{os.getenv('KFP_DATA_GENERATION_BASE_IMAGE_NAME')}"
    f":{os.getenv('KFP_DATA_GENERATION_BASE_IMAGE_TAG')}"
)

INDEXING_BASE_IMAGE = (
    f"{os.getenv('KFP_IMAGE_REGISTRY')}"
    f"/{os.getenv('KFP_INDEXING_BASE_IMAGE_NAME')}"
    f":{os.getenv('KFP_INDEXING_BASE_IMAGE_TAG')}"
)

ANALYSIS_BASE_IMAGE = (
    f"{os.getenv('KFP_IMAGE_REGISTRY')}"
    f"/{os.getenv('KFP_ANALYSIS_BASE_IMAGE_NAME')}"
    f":{os.getenv('KFP_ANALYSIS_BASE_IMAGE_TAG')}"
)


##############################################################################
# Logging setup
##############################################################################

def setup_logging():
    """Configures logging for KFP component pods.

    ``logging.basicConfig`` is a no-op when handlers are already present
    (KFP executor pre-configures them before the component body runs).
    Calling ``setLevel`` on the root logger overrides the level regardless.
    """
    import logging
    _level = os.environ.get('LOGLEVEL', 'INFO').upper()
    logging.basicConfig(level=_level)
    logging.getLogger().setLevel(_level)


##############################################################################
# Git URL helper
##############################################################################

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


##############################################################################
# Secret-injection decorator
##############################################################################

def inject_secret_as_env(secret_name: str):
    """Decorator factory that injects ALL keys from a K8s secret as env vars.

    Args:
        secret_name: Name of the Kubernetes Secret whose keys should be
                     exposed as environment variables of the same name.
    """
    import functools

    def read_secret_keys() -> list:
        """Returns the data keys of the secret, or [] on any error.
        """
        try:

            from kubernetes import client as k8s_client, config as k8s_config

            try:
                k8s_config.load_incluster_config()
            except Exception:
                k8s_config.load_kube_config()

            namespace = os.getenv("KFP_NAMESPACE", "default")

            secret = k8s_client.CoreV1Api().read_namespaced_secret(
                name=secret_name, namespace=namespace
            )

            return list((secret.data or {}).keys())

        except Exception:

            return []

    def decorator(component_fn):

        @functools.wraps(component_fn)
        def wrapper(*args, **kwargs):
            task = component_fn(*args, **kwargs)
            try:
                from kfp import kubernetes

                keys = read_secret_keys()

                if keys:
                    kubernetes.use_secret_as_env(
                        task,
                        secret_name=secret_name,
                        secret_key_to_env={k: k for k in keys},
                    )

            except ImportError:
                pass

            return task

        return wrapper

    return decorator


##############################################################################
# Pipeline compilation helper
##############################################################################

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


##############################################################################
# Artifact I/O context managers
##############################################################################

@contextmanager
def read_from_input_artifact(artifact):
    """Extract a KFP Input[Dataset] tar.gz archive to a temp dir.
    """
    import shutil, tarfile, tempfile

    tmp = tempfile.mkdtemp()

    try:

        with tarfile.open(artifact.path, "r:gz") as tar:
            tar.extractall(tmp)

        yield tmp

    finally:

        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def write_to_output_artifact(artifact, compresslevel=1):
    """Yield a fresh temp dir; archive it to a KFP Output[Dataset] on clean exit.

    Args:
        artifact:      KFP Output[Dataset] whose ``.path`` receives the archive.
        compresslevel: gzip compression level (1 = fastest, 9 = smallest).
                       Defaults to 1; switch to 0 / ``"w:"`` mode for binary
                       data (e.g. parquet + embeddings) that compresses poorly.
    """
    import os, shutil, tarfile, tempfile

    tmp = tempfile.mkdtemp()

    try:

        yield tmp

        os.makedirs(os.path.dirname(artifact.path), exist_ok=True)

        with tarfile.open(artifact.path, "w:gz", compresslevel=compresslevel) as tar:
            tar.add(tmp, arcname=".")

    finally:

        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def use_ephemeral_space():
    """Yield a temporary directory and remove it on exit."""
    import shutil, tempfile

    tmp = tempfile.mkdtemp()

    try:

        yield tmp

    finally:

        shutil.rmtree(tmp, ignore_errors=True)
