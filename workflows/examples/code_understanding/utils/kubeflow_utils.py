import os
import sys
import logging
from contextlib import contextmanager

##############################################################################
# Logging setup
##############################################################################

def setup_logging(level=None):
    """Configures logging to stdout with clean formatting and removes file handlers."""
    log_level = level or os.environ.get("LOGLEVEL", "INFO").upper()
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
    logging.basicConfig(
        level=log_level,
        format="[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )
    for logger in logging.Logger.manager.loggerDict.values():
        if isinstance(logger, logging.Logger):
            for h in list(logger.handlers):
                if isinstance(h, logging.FileHandler):
                    h.close()
            logger.handlers = [h for h in logger.handlers if not isinstance(h, logging.FileHandler)]


def get_logger(name: str = None):
    """Returns a logger instance ensuring unified console logging configuration."""
    setup_logging()
    return logging.getLogger(name)


setup_logging()

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
        """Returns the data keys of the secret, or [] on any error."""
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
            import logging
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
                try:
                    # 1. Mount OpenShift's trusted CA bundle where launcher_v2 expects it
                    kubernetes.use_config_map_as_volume(
                        task,
                        config_map_name="odh-trusted-ca-bundle",
                        mount_path="/etc/pki/ca-trust/extracted/pem/"
                    )
                    ca_path = "/etc/pki/ca-trust/extracted/pem/ca-bundle.crt"
                    task.set_env_variable("GIT_SSL_CAINFO", ca_path)
                    task.set_env_variable("PIP_CERT", ca_path)
                    task.set_env_variable("SSL_CERT_FILE", ca_path)
                    task.set_env_variable("REQUESTS_CA_BUNDLE", ca_path)
                except Exception as e:
                    logging.error(f"KFP use_as_configmap failed: {e}")
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
    """Extract a KFP Input[Dataset] tar.gz archive to a temp dir."""
    import tarfile, tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(artifact.path, "r:gz") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(tmp, filter="data")
            else:
                tar.extractall(tmp)
        yield tmp


@contextmanager
def write_to_output_artifact(artifact, compresslevel=1):
    """Yield a fresh temp dir; archive it to a KFP Output[Dataset] on clean exit.

    Args:
        artifact:      KFP Output[Dataset] whose ``.path`` receives the archive.
        compresslevel: gzip compression level (1 = fastest, 9 = smallest).
                       Defaults to 1; switch to 0 / ``"w:"`` mode for binary
                       data (e.g. parquet + embeddings) that compresses poorly.
    """
    import os, tarfile, tempfile

    with tempfile.TemporaryDirectory() as tmp:
        yield tmp
        os.makedirs(os.path.dirname(artifact.path), exist_ok=True)
        with tarfile.open(artifact.path, "w:gz", compresslevel=compresslevel) as tar:
            tar.add(tmp, arcname=".")


@contextmanager
def use_ephemeral_space():
    """Yield a temporary directory and remove it on exit."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        yield tmp
