import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Output
from utils.kubeflow_utils import INDEXING_BASE_IMAGE, inject_secret_as_env


##############################################################################
# Components
##############################################################################

@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, install_kfp_package=False)
def validate_focal_repo_op(git_repo: str, git_branch: str):
    """Fails the run before any work when the focal repository is not in the repository list."""

    import json
    import os
    from utils.kubeflow_utils import setup_logging
    from utils.repo_catalog import require_focal_in_list
    setup_logging()

    require_focal_in_list(git_repo, git_branch, json.loads(os.getenv("GIT_REPO_LIST_CONTENTS")))


@inject_secret_as_env(secret_name="code-understanding-env")
@inject_secret_as_env(secret_name="git-credentials")
@dsl.component(base_image=INDEXING_BASE_IMAGE, install_kfp_package=False)
def annotate_focal_repo_op(git_repo: str, git_branch: str, parent_target_path: str,
                           codebase_dir: Output[Dataset]):
    """Saves the repository catalog and outputs the focal repository's annotated generated data."""

    import json
    import os
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact
    from utils.loader_utils import download_code_metadata_directories
    from utils.repo_catalog import prepare_focal_codebase
    setup_logging()

    git_repos = json.loads(os.getenv("GIT_REPO_LIST_CONTENTS"))
    download_code_metadata_directories(git_repos, parent_target_path)
    # annotate_focal_repo deletes and recreates the temp dir; it only has to exist again when archived.
    with write_to_output_artifact(codebase_dir) as tmp_codebase:
        prepare_focal_codebase(git_repo, git_branch, git_repos, parent_target_path, tmp_codebase)
