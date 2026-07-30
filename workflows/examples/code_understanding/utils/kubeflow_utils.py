"""Context managers for KFP component tar artifact I/O.

Each component that exchanges data through KFP's MinIO-backed Dataset artifacts
should use these helpers instead of writing the tar/tempdir boilerplate inline.

Usage inside a @dsl.component function body::

    from utils.kubeflow_utils import read_from_input_artifact, write_to_output_artifact

    # Input only
    with read_from_input_artifact(source_dir) as tmp_source:
        ...

    # Output only
    with write_to_output_artifact(target_dir) as tmp_target:
        ...

    # Input + output (combined with statement)
    with read_from_input_artifact(source_dir) as tmp_src, write_to_output_artifact(target_dir) as tmp_dst:
        ...

    # Scratch directory (no artifact, just guaranteed cleanup)
    with use_ephemeral_space() as tmp:
        ...
"""
from contextlib import contextmanager


@contextmanager
def read_from_input_artifact(artifact):
    """Extract a KFP Input[Dataset] tar.gz archive to a temp dir.

    Yields the path of the extracted directory.  The directory is removed on
    exit regardless of whether an exception was raised.
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

    If the body raises an exception the archive step is skipped (so the output
    artifact is not written with partial data).  The temp dir is removed in
    either case.

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
    """Yield a temporary directory and remove it on exit.

    Use this for scratch directories that are not backed by a KFP artifact
    (e.g. a ``target_path`` passed to a base pipeline helper that the caller
     does not need to persist).
    """
    import shutil, tempfile

    tmp = tempfile.mkdtemp()

    try:

        yield tmp

    finally:

        shutil.rmtree(tmp, ignore_errors=True)
