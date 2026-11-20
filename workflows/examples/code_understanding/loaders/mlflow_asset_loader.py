import json
import logging
import os
import posixpath
import sys
import threading
import traceback

_MLFLOW_IMPORT_LOCK = threading.RLock()


def _ensure_mlflow_imported():
    """Import mlflow under a re-entrant lock and heal orphaned sys.modules state."""
    with _MLFLOW_IMPORT_LOCK:
        try:
            import mlflow as _mlflow
            try:
                import mlflow.version as _mlflow_version
                if not hasattr(_mlflow, "version"):
                    _mlflow.version = _mlflow_version
            except ModuleNotFoundError:
                pass
            from mlflow.tracking import MlflowClient as _MlflowClient
            return _mlflow, _MlflowClient
        except AttributeError:
            # If an earlier concurrent import failed mid-way, sys.modules may retain
            # submodules like 'mlflow.version' without 'mlflow', causing subsequent
            # imports to fail with "partially initialized module 'mlflow' has no
            # attribute 'version'". Purge half-initialized mlflow modules and retry
            # synchronously under the lock.
            for mod_name in [k for k in list(sys.modules.keys()) if k == "mlflow" or k.startswith("mlflow.")]:
                sys.modules.pop(mod_name, None)
            import mlflow as _mlflow
            try:
                import mlflow.version as _mlflow_version
                if not hasattr(_mlflow, "version"):
                    _mlflow.version = _mlflow_version
            except ModuleNotFoundError:
                pass
            from mlflow.tracking import MlflowClient as _MlflowClient
            return _mlflow, _MlflowClient


mlflow, MlflowClient = _ensure_mlflow_imported()

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

from .asset_loader import AssetLoader
from utils.pipeline_utils import (
    escape_mlflow_filter_value,
    is_mlflow_tracking_request as _is_mlflow_tracking_request,
    patch_mlflow_session_forwarded_token as _patch_session_forwarded_token,
)
from utils.request_timing import timing_span, timed, timing_enabled
from utils.artifact_measurement import measure_payload, upload_identity

_REPO_CATALOG_LOCK = threading.Lock()


def _repo_identity(entry: dict) -> tuple[str, str]:
    url = str(entry.get("git_repo", "")).strip().rstrip("/").removesuffix(".git")
    branch = str(entry.get("git_branch") or "main").strip() or "main"
    return (url, branch)


def _repo_slug(git_repo: str, git_branch: str = "main") -> str:
    branch = (git_branch or "main").strip() or "main"
    if not (git_repo or "").strip():
        return ""
    try:
        from utils.code_utils import generate_slug_from_repo

        return generate_slug_from_repo(git_repo, branch)
    except Exception:
        from urllib.parse import urlparse

        path = urlparse(git_repo.strip()).path.strip("/").removesuffix(".git")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[-2]}-{parts[-1]}-{branch}"[:255]
        return f"{parts[0]}-{branch}"[:255] if parts else ""


def _merge_repo_catalog(base: list, incoming: list) -> list[dict]:
    """Merge incoming repo dicts into base keyed by (git_repo, git_branch), preserving credentials."""
    merged: list[dict] = []
    index_by_key: dict[tuple[str, str], int] = {}
    for raw in (base or []):
        if not isinstance(raw, dict) or not raw.get("git_repo"):
            continue
        entry = dict(raw)
        key = _repo_identity(entry)
        index_by_key[key] = len(merged)
        merged.append(entry)
    for raw in (incoming or []):
        if not isinstance(raw, dict) or not raw.get("git_repo"):
            continue
        key = _repo_identity(raw)
        if key in index_by_key:
            existing = merged[index_by_key[key]]
            existing["git_repo"] = raw["git_repo"]
            existing["git_branch"] = raw.get("git_branch") or existing.get("git_branch") or "main"
            if raw.get("git_token"):
                existing["git_username"] = str(raw.get("git_username") or "")
                existing["git_token"] = raw["git_token"]
        else:
            entry = {"git_repo": raw["git_repo"], "git_branch": raw.get("git_branch") or "main"}
            if raw.get("git_token"):
                entry["git_username"] = str(raw.get("git_username") or "")
                entry["git_token"] = raw["git_token"]
            index_by_key[key] = len(merged)
            merged.append(entry)
    return merged


def _remove_from_repo_catalog(base: list, git_repo: str, git_branch: str = "main") -> list[dict]:
    """Return a copy of *base* without entries matching ``(git_repo, git_branch)``."""
    target = _repo_identity({"git_repo": git_repo, "git_branch": git_branch})
    return [
        dict(raw)
        for raw in (base or [])
        if isinstance(raw, dict) and raw.get("git_repo") and _repo_identity(raw) != target
    ]


def _run_tags(run) -> dict:
    return getattr(getattr(run, "data", None), "tags", None) or {}


def _search_all_runs(
    client,
    experiment_ids: list[str],
    filter_string: str = "",
    order_by: list[str] | None = None,
) -> list:
    # RHOAI MLflow servers predate `IS NULL` filter clauses; paginate and filter missing tags in Python.
    results: list = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        kwargs: dict[str, object] = {
            "experiment_ids": experiment_ids,
            "filter_string": filter_string,
            "max_results": 1000,
        }
        if order_by:
            kwargs["order_by"] = order_by
        if page_token:
            kwargs["page_token"] = page_token
        runs = client.search_runs(**kwargs)
        results.extend(runs or [])
        next_token = getattr(runs, "token", None)
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        page_token = next_token
    return results


class MlFlowAssetLoader(AssetLoader):
    """Loads an asset from the MLflow artifacts registry."""

    _RUN_NAME = "code-understanding"
    _SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"

    def __init__(self, namespace: str | None = None, workspace: str | None = None):
        super().__init__(namespace=namespace, workspace=workspace)

        if not os.environ.get("MLFLOW_TRACKING_TOKEN") and os.path.exists(self._SA_TOKEN_PATH):
            try:
                with open(self._SA_TOKEN_PATH) as f:
                    logging.info("Setting MLFLOW_TRACKING_TOKEN from Kubernetes service account token...")
                    os.environ["MLFLOW_TRACKING_TOKEN"] = f.read().strip()
            except Exception as e:
                logging.debug(f"Failed to read SA token: {e}")

        if os.environ.get("MLFLOW_TRACKING_TOKEN"):
            _patch_session_forwarded_token()

        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI")
        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)

    def _get_global_enrichment_runs(
        self,
        client,
        experiment_id: str,
        base_filter: str,
        null_tag_keys: tuple[str, ...] = ("git_slug",),
    ) -> list:
        """Returns the ordered list of global enrichment runs to merge: [checkpoint] + [deltas].

        The checkpoint run (tagged latest=true) is first; delta runs (no latest tag, created
        after the checkpoint) follow in chronological order. Only unarchived runs are returned.
        """
        runs = _search_all_runs(
            client,
            experiment_ids=[experiment_id],
            filter_string=base_filter,
            order_by=["attributes.start_time ASC"],
        )
        unarchived = [
            r
            for r in runs
            if (t := _run_tags(r)).get("archived") != "true"
            and all(not t.get(k) for k in null_tag_keys)
        ]
        checkpoints = [r for r in unarchived if _run_tags(r).get("latest") == "true"]
        checkpoint = checkpoints[-1] if checkpoints else None
        min_start = (
            getattr(getattr(checkpoint, "info", None), "start_time", None) if checkpoint else None
        )
        deltas = [
            r
            for r in unarchived
            if "latest" not in _run_tags(r)
            and (min_start is None or getattr(getattr(r, "info", None), "start_time", 0) >= min_start)
        ]
        return ([checkpoint] if checkpoint else []) + deltas

    def _mark_as_archived(self, client, anchor_run) -> None:
        """Archives all unarchived global enrichment runs created before anchor_run."""
        stale = _search_all_runs(
            client,
            experiment_ids=[anchor_run.info.experiment_id],
            filter_string=(
                "tags.category = 'enrichments'"
                f" AND attributes.start_time <= {anchor_run.info.start_time}"
            ),
        )
        for stale_run in stale:
            if stale_run.info.run_id == anchor_run.info.run_id:
                continue
            tags = _run_tags(stale_run)
            if tags.get("git_slug") or tags.get("archived") == "true":
                continue
            client.set_tag(stale_run.info.run_id, "archived", "true")

    @staticmethod
    def _has_prior_enrichment_checkpoint(runs: list) -> bool:
        """Return True if any run in the list is a checkpoint (tagged latest=true)."""
        return any(_run_tags(r).get("latest") == "true" for r in runs)

    def _get_artifact_base_uri(self, client, experiment, filter_string: str) -> str:
        """Return the artifact URI of the latest matching run in *experiment*."""
        latest_runs = client.search_runs(
            experiment_ids=[experiment.experiment_id],
            filter_string=filter_string,
            order_by=["attributes.start_time DESC"],
            max_results=1,
        )
        return latest_runs[0].info.artifact_uri if latest_runs else experiment.artifact_location

    @timed("mlflow.artifact_uri_lookup", "mlflow")
    def _get_absolute_artifact_uri(self,
                                   asset_file_path: str,
                                   experiment_name: str,
                                   tags: dict = None):
        uris = self._get_absolute_artifact_uris(asset_file_path, experiment_name=experiment_name, tags=tags)
        return uris[-1]

    def _get_absolute_artifact_uris(
        self, asset_file_path: str, experiment_name: str, tags: dict = None
    ) -> list[str]:
        from utils.loader_utils import is_global_scoped_enrichment_tagset

        client = MlflowClient()
        experiment = self.get_or_create_experiment_by_name(client, experiment_name)

        tag_filter_parts = []
        null_tag_keys = []
        for k, v in (tags or {}).items():
            if v is None:
                null_tag_keys.append(k)
            elif v != "":
                tag_filter_parts.append(f"tags.\"{k}\" = '{escape_mlflow_filter_value(v)}'")

        if is_global_scoped_enrichment_tagset(tags):
            enrichment_experiment = self.get_or_create_experiment_by_name(
                client, self.RESULT_DIRECTORY_ASSET_EXPERIMENT
            )
            base_filter = " AND ".join(tag_filter_parts)
            runs = self._get_global_enrichment_runs(
                client,
                enrichment_experiment.experiment_id,
                base_filter,
                tuple(null_tag_keys),
            )
            if not runs:
                return [posixpath.join(enrichment_experiment.artifact_location, asset_file_path)]
            enrichment_uris = [
                posixpath.join(run.info.artifact_uri, asset_file_path) for run in runs
            ]
            if not self._has_prior_enrichment_checkpoint(runs):
                static_experiment = self.get_or_create_experiment_by_name(
                    client, self.STATIC_ASSET_EXPERIMENT
                )
                static_base_uri = self._get_artifact_base_uri(
                    client, static_experiment, "tags.latest = 'true'"
                )
                default_static_name = getattr(self, "DEFAULT_STATIC_ASSET_EXPERIMENT", None)
                if (
                    static_base_uri == static_experiment.artifact_location
                    and default_static_name
                    and default_static_name != self.STATIC_ASSET_EXPERIMENT
                ):
                    default_static_exp = self.get_or_create_experiment_by_name(
                        client, default_static_name
                    )
                    static_base_uri = self._get_artifact_base_uri(
                        client, default_static_exp, "tags.latest = 'true'"
                    )
                enrichment_uris.insert(0, posixpath.join(static_base_uri, asset_file_path))
            return enrichment_uris

        static_exps = {
            self.STATIC_ASSET_EXPERIMENT,
            getattr(self, "DEFAULT_STATIC_ASSET_EXPERIMENT", self.STATIC_ASSET_EXPERIMENT),
        }
        filter_string = (
            "tags.latest = 'true'"
            if experiment_name in static_exps and not tags
            else " AND ".join(tag_filter_parts)
        )

        logging.debug(f"Filter string: {filter_string}")
        asset_base_uri = self._get_artifact_base_uri(client, experiment, filter_string)
        logging.info(f"Base absolute storage URI: {asset_base_uri}")
        return [posixpath.join(asset_base_uri, asset_file_path)]

    def _mark_as_latest(self, client, experiment_id, new_run_id, tags: dict = None):
        filter_parts = ["tags.latest = 'true'"]
        null_tag_keys = []
        if tags:
            for k, v in tags.items():
                if v is None:
                    null_tag_keys.append(k)
                elif v != "":
                    filter_parts.append(f"tags.\"{k}\" = '{escape_mlflow_filter_value(v)}'")

        existing = _search_all_runs(
            client,
            experiment_ids=[experiment_id],
            filter_string=" AND ".join(filter_parts),
        )
        for run in existing:
            if null_tag_keys and any(_run_tags(run).get(k) for k in null_tag_keys):
                continue
            client.set_tag(run.info.run_id, "latest", "false")
        client.set_tag(new_run_id, "latest", "true")

    def get_or_create_experiment_by_name(self, client, experiment_name):
        """Gets or creates an MLflow experiment by name, handling deleted and non-active states."""
        experiment = client.get_experiment_by_name(experiment_name)
        if experiment:
            if experiment.lifecycle_stage == "deleted":
                logging.info(f"Experiment '{experiment_name}' is deleted. Restoring.")
                client.restore_experiment(experiment.experiment_id)
                experiment = client.get_experiment(experiment.experiment_id)
            elif experiment.lifecycle_stage != "active":
                logging.info(f"Experiment '{experiment_name}' is not active. Deleting and recreating.")
                client.delete_experiment(experiment.experiment_id)
                experiment = client.get_experiment(client.create_experiment(name=experiment_name))
            return experiment
        return client.get_experiment(client.create_experiment(name=experiment_name))

    def _get_download_candidate_metadata(
        self,
        asset_path: str,
        git_slug: str | None,
        experiment_name: str | None,
        asset_tags: dict | None,
    ) -> list:
        """Returns the ordered list of (path, experiment_name, tags) candidates to try."""
        from utils.loader_utils import is_global_scoped_enrichment

        # If caller explicitly requests a non-static experiment (e.g. results or result-directories),
        # try that direct candidate without prepending an enrichments lookup.
        target_exp = experiment_name or self.STATIC_ASSET_EXPERIMENT
        if experiment_name and experiment_name != self.STATIC_ASSET_EXPERIMENT:
            return [(asset_path, target_exp, asset_tags)]

        enrichment_path = asset_path
        if not enrichment_path.startswith(AssetLoader.RESULTS_PATH_PREFIX_ENRICHMENTS):
            prefix = f"{AssetLoader.RESULTS_PATH_PREFIX_ENRICHMENTS}/.assets"
            enrichment_path = f"{prefix}/{enrichment_path}"
        enrichment_tags = {"category": "enrichments"}
        if is_global_scoped_enrichment(enrichment_path):
            enrichment_tags["git_slug"] = None
        elif git_slug:
            enrichment_tags["git_slug"] = git_slug
        default_static_exp = getattr(
            self, "DEFAULT_STATIC_ASSET_EXPERIMENT", self.STATIC_ASSET_EXPERIMENT
        )
        return [
            (enrichment_path, self.RESULT_DIRECTORY_ASSET_EXPERIMENT, enrichment_tags),
            (asset_path, target_exp, asset_tags),
            *(
                [(asset_path, default_static_exp, asset_tags)]
                if target_exp == self.STATIC_ASSET_EXPERIMENT
                and default_static_exp != self.STATIC_ASSET_EXPERIMENT
                else []
            ),
        ]

    def _download_and_merge_enrichments(self, uris: list[str], dst_path: str = None):
        """Downloads and merges content across multiple artifact URIs."""
        import shutil
        from utils.loader_utils import merge_json_enrichment

        merged = None
        latest_path = None
        for uri in uris:
            try:
                with timing_span("mlflow.artifact_download", "io"):
                    path = mlflow.artifacts.download_artifacts(artifact_uri=uri)
                if not os.path.exists(path):
                    continue
                if dst_path is not None:
                    os.makedirs(dst_path, exist_ok=True)
                    dest_file = os.path.join(dst_path, os.path.basename(path))
                    shutil.copy2(path, dest_file)
                    path = dest_file
                with open(path, "r") as f:
                    content = json.load(f) if uri.endswith(".json") else f.read()
                merged = content if merged is None else merge_json_enrichment(merged, content)
                latest_path = path
            except Exception:
                logging.info(
                    "_download_and_merge_enrichments: skipping URI %s: %s",
                    uri,
                    traceback.format_exc(),
                )

        if latest_path and merged is not None and len(uris) > 1:
            with open(latest_path, "w") as f:
                json.dump(merged, f) if isinstance(merged, (dict, list)) else f.write(merged)

        return merged

    def download(self,
                 asset_file_path: str,
                 download_dir: str = None,
                 experiment_name=None,
                 asset_tags: dict = None,
                 git_slug: str = None):
        """Downloads and returns the asset from the MLflow artifacts registry."""
        candidates = self._get_download_candidate_metadata(
            asset_file_path, git_slug, experiment_name, asset_tags
        )

        for path, exp_name, tags in candidates:
            try:
                asset_uris = self._get_absolute_artifact_uris(
                    path, experiment_name=exp_name, tags=tags
                )
                merged = self._download_and_merge_enrichments(asset_uris, dst_path=download_dir)
                if merged is not None:
                    return merged
            except Exception:
                logging.info("Asset %s not found, trying next candidate", path)
                logging.debug(traceback.format_exc())

        return None

    def download_dir(self,
                     asset_dir_path: str,
                     download_dir: str,
                     experiment_name=None,
                     asset_tags: dict = None,
                     git_slug: str = None):
        """Downloads a directory from the MLflow artifacts registry to a local directory."""
        import shutil

        candidates = self._get_download_candidate_metadata(
            asset_dir_path, git_slug, experiment_name, asset_tags
        )

        last_err = None
        for path, exp_name, tags in candidates:
            try:
                asset_uris = self._get_absolute_artifact_uris(
                    path, experiment_name=exp_name, tags=tags
                )
                copied_any = False
                uri_err = None
                for uri in asset_uris:
                    try:
                        with timing_span("mlflow.artifact_download", "io"):
                            local_path = mlflow.artifacts.download_artifacts(artifact_uri=uri)
                        os.makedirs(download_dir, exist_ok=True)
                        with timing_span("mlflow.artifact_copy", "io"):
                            shutil.copytree(local_path, download_dir, dirs_exist_ok=True)
                        copied_any = True
                    except Exception as e:
                        uri_err = e
                        if len(asset_uris) == 1:
                            raise
                if not copied_any:
                    raise uri_err or FileNotFoundError(f"Asset {asset_dir_path} not found.")

                enrichment_prefix = os.path.join(".enrichments", ".assets")
                if len(asset_uris) > 1:
                    for root, _, files in os.walk(download_dir):
                        for f in files:
                            full_path = os.path.join(root, f)
                            if enrichment_prefix not in full_path:
                                continue
                            rel_path = os.path.relpath(full_path, download_dir).replace("\\", "/")
                            file_uris = self._get_absolute_artifact_uris(
                                f"{path}/{rel_path}", experiment_name=exp_name, tags=tags
                            )
                            merged = self._download_and_merge_enrichments(file_uris)
                            if merged is not None:
                                with open(full_path, "w") as fp:
                                    if isinstance(merged, (dict, list)):
                                        json.dump(merged, fp)
                                    else:
                                        fp.write(merged)
                return True
            except Exception as e:
                last_err = e
                logging.info("Asset %s not found, trying next candidate", path)
                logging.debug(traceback.format_exc())

        logging.error(f"Error downloading asset directory {asset_dir_path}: {last_err}")
        if last_err:
            raise last_err
        raise FileNotFoundError(f"Asset {asset_dir_path} not found.")

    def load_results(
        self,
        download_dir: str,
        artifact_path: str = None,
        tags: dict = None,
        git_slug: str | None = None,
    ):
        """Downloads a previously logged result directory from MLflow to download_dir."""
        try:
            result = self.download_dir(
                artifact_path,
                download_dir,
                experiment_name=self.RESULT_DIRECTORY_ASSET_EXPERIMENT,
                asset_tags=tags,
                git_slug=git_slug,
            )
            logging.info("Loaded prior results from %s to %s", artifact_path, download_dir)
            return result
        except Exception as e:
            logging.info("No prior results to load from %s: %s", artifact_path, e)
            return None

    @timed("mlflow.log_results", "mlflow")
    def log_results(self, results_path: str, artifact_path: str = None, tags: dict = None,
                    content: str = None):
        """Logs pipeline output artifacts to a new MLflow run."""
        try:
            is_dir = os.path.isdir(results_path)

            if "pipeline_run_id" not in (tags or {}):
                from utils.pipeline_utils import get_pipeline_run_id

                pipeline_run_id = get_pipeline_run_id()
                if pipeline_run_id:
                    tags = {**(tags or {}), "pipeline_run_id": pipeline_run_id}

            if os.path.dirname(results_path):
                os.makedirs(os.path.dirname(results_path), exist_ok=True)

            if content is not None and not is_dir:
                with open(results_path, "w") as f:
                    f.write(content)

            client = MlflowClient()
            experiment_name = self.RESULT_DIRECTORY_ASSET_EXPERIMENT if is_dir else self.RESULT_ASSET_EXPERIMENT

            with timing_span("mlflow.experiment_lookup", "mlflow"):
                experiment = self.get_or_create_experiment_by_name(client, experiment_name)

            fields = {}
            if timing_enabled():
                fields.update(upload_identity(results_path, artifact_path))
                with timing_span("mlflow.payload_measurement", "io", **fields) as measurement:
                    fields.update(measure_payload(results_path))
                    measurement.update(fields)

            active_run = mlflow.active_run()
            if active_run:
                prior_fields = dict(fields)
                if timing_enabled():
                    try:
                        prior_fields["prior_mlflow_run_id"] = active_run.info.run_id
                    except Exception:
                        pass
                with timing_span("mlflow.prior_run_end", "mlflow", **prior_fields):
                    mlflow.end_run()

            with timing_span("mlflow.run_lifecycle", "mlflow", **fields) as lifecycle:
                with timing_span("mlflow.run_create", "mlflow", **fields) as creation:
                    run_context = mlflow.start_run(experiment_id=experiment.experiment_id)
                    if timing_enabled():
                        try:
                            fields.update(mlflow_run_id=run_context.info.run_id, experiment_id=str(experiment.experiment_id))
                        except Exception:
                            pass
                        creation.update(fields)
                with run_context as run:
                    if timing_enabled():
                        try:
                            fields.update(mlflow_run_id=run.info.run_id, experiment_id=str(experiment.experiment_id))
                        except Exception:
                            pass
                        lifecycle.update(fields)

                    result_tags = dict(tags or {})
                    if kfp_run_id := os.environ.get("KFP_RUN_ID"):
                        result_tags.setdefault("kfp_run_id", kfp_run_id)
                    if result_tags:
                        with timing_span("mlflow.run_tags", "mlflow", **fields):
                            mlflow.set_tags(result_tags)

                    with timing_span("mlflow.artifact_upload", "io", **fields):
                        if is_dir:
                            mlflow.log_artifacts(results_path, artifact_path=artifact_path)
                        else:
                            mlflow.log_artifact(results_path, artifact_path=artifact_path)

                    logging.info(f"Logged results to run {run.info.run_id}")
                    return run.info.run_id

        except Exception as e:
            logging.error(f"Error logging results {results_path}: {e}")
            raise e

    def register_static_asset(
        self,
        results_path: str,
        artifact_path: str = None,
        tags: dict = None,
        content: str = None,
    ):
        """Logs an artifact to STATIC_ASSET_EXPERIMENT so ``download`` can retrieve it."""
        import tempfile

        try:
            is_dir = os.path.isdir(results_path)

            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
                if content is not None and not is_dir:
                    log_path = os.path.join(tmp_dir, os.path.basename(results_path))
                    with open(log_path, "w") as f:
                        f.write(content)
                else:
                    log_path = results_path
                    if os.path.dirname(results_path):
                        os.makedirs(os.path.dirname(results_path), exist_ok=True)

                client = MlflowClient()
                experiment = self.get_or_create_experiment_by_name(
                    client, self.STATIC_ASSET_EXPERIMENT
                )

                if mlflow.active_run():
                    mlflow.end_run()

                with mlflow.start_run(experiment_id=experiment.experiment_id) as run:
                    if tags:
                        mlflow.set_tags(tags)
                    if is_dir:
                        mlflow.log_artifacts(log_path, artifact_path=artifact_path)
                    else:
                        mlflow.log_artifact(log_path, artifact_path=artifact_path)
                    logging.info(f"Logged asset to run {run.info.run_id}")
                    return run.info.run_id
        except Exception as e:
            logging.error(f"Error logging asset {results_path}: {e}")
            raise e

    def get_prompt_name(self, prompt_path: str) -> str:
        """Derives the MLflow prompt registry name by replacing path separators with dashes and using a double-dash to delimit the directory from the filename."""
        prompt_name = "--".join(prompt_path.rsplit("/", 1))
        prompt_name = prompt_name.replace("/", "-")
        return prompt_name

    @staticmethod
    def _prompt_api():
        """Returns the MLflow module exposing prompt registry calls (mlflow or mlflow.genai)."""
        if hasattr(mlflow, "register_prompt"):
            return mlflow
        return getattr(mlflow, "genai", mlflow)

    def upload_prompt(self, prompt_path: str):
        """Registers a prompt template from the local assets directory to the MLflow prompt registry."""
        try:
            asset_uri = os.path.join(self._PROMPTS_DIR, prompt_path + ".txt")
            with open(asset_uri, "r") as f:
                content = f.read()

            name = self.get_prompt_name(prompt_path)
            self._prompt_api().register_prompt(name=name, template=content)
            logging.info(f"Registered prompt '{name}'")
        except Exception as e:
            logging.error(f"Error registering prompt {prompt_path}: {e}")
            raise e

    def load_skill(self, name: str) -> tuple[str, dict]:
        """Loads a skill from the MLflow prompt registry."""
        try:
            prompt_name = self.get_prompt_name(f"skills/{name}")
            prompt = self._prompt_api().load_prompt(prompt_name)
            body, meta = self._get_prompt_body_and_metadata(prompt.template)
            return body, meta
        except Exception as e:
            logging.error(f"Error loading skill {name}: {e}")
            raise e

    def download_prompt(self, prompt_path: str, **kwargs) -> tuple[str, dict]:
        """Loads a prompt from the MLflow prompt registry and renders it with the provided variables."""
        try:
            from jinja2 import Template

            name = self.get_prompt_name(prompt_path)
            prompt = self._prompt_api().load_prompt(name)
            body, meta = self._get_prompt_body_and_metadata(prompt.template)
            return Template(body).render(**kwargs), meta
        except Exception as e:
            logging.error(f"Error loading prompt {prompt_path}: {e}")
            raise e

    def register_enrichments(
        self,
        repo_dir: str | None = None,
        git_slug: str | None = None,
        enrichment_files: dict | None = None,
    ) -> str:
        """Persists the enrichments directory to the MLflow pipeline asset store."""
        import contextlib
        import tempfile as _tmp
        from utils.loader_utils import (
            get_global_scoped_enrichment_dir,
            sync_enrichments,
        )

        _ctx = (
            _tmp.TemporaryDirectory(ignore_cleanup_errors=True)
            if repo_dir is None
            else contextlib.nullcontext(repo_dir)
        )

        with _ctx as _repo_dir:
            enrichments_dir = os.path.join(_repo_dir, ".enrichments")
            assets_dir = get_global_scoped_enrichment_dir(_repo_dir)
            os.makedirs(enrichments_dir, exist_ok=True)

            if not git_slug:
                sync_enrichments(self, assets_dir)

            if enrichment_files:
                for rel_path, content in enrichment_files.items():
                    dst = os.path.join(assets_dir, rel_path)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, "w") as f:
                        json.dump(content, f) if isinstance(content, (dict, list)) else f.write(str(content))

            tags = {"category": "enrichments"}
            if git_slug:
                tags["git_slug"] = git_slug
            else:
                tags["latest"] = "true"

            client = MlflowClient()
            if git_slug and os.path.exists(assets_dir):
                try:
                    self.log_results(
                        assets_dir,
                        artifact_path=f"{self.RESULTS_PATH_PREFIX_ENRICHMENTS}/.assets",
                        tags={"category": "enrichments"},
                    )
                except Exception as e:
                    logging.warning("Failed to log assets_dir enrichment run; continuing: %s", e)

            run_id = self.log_results(
                enrichments_dir,
                artifact_path=self.RESULTS_PATH_PREFIX_ENRICHMENTS,
                tags=tags,
            )
            anchor_run = client.get_run(run_id)

            if not git_slug:
                self._mark_as_archived(client, anchor_run)

        return f"Logged enrichments from {repo_dir or 'temp directory'}"

    def log_repos(self, run_repos: list) -> None:
        """Logs the repo list to the MLflow asset store: first as an enrichment under
        repos/repo_list.json, then under category: repositories.
        """
        import tempfile
        from utils.pipeline_utils import get_pipeline_run_id

        pipeline_run_id = get_pipeline_run_id(allow_generate=True)

        # shortcut: process-local lock, upgrade to optimistic MLflow tag CAS if scaling >1 replica
        with _REPO_CATALOG_LOCK:
            try:
                base = self.download("repos/repo_list.json")
                if base is None:
                    base = self.download("repos/repo_list1.json")
                if base is None:
                    base = []
                catalog_repos = _merge_repo_catalog(base, run_repos)
                self.register_enrichments(enrichment_files={"repos/repo_list.json": catalog_repos})
            except Exception:
                logging.warning("Failed to update repo catalog enrichment", exc_info=True)

        repo_tags = {"category": "repositories"}
        if pipeline_run_id:
            repo_tags["pipeline_run_id"] = pipeline_run_id

        public_repos = [
            {"git_repo": r["git_repo"], "git_branch": r.get("git_branch") or "main"}
            for r in (run_repos or [])
            if isinstance(r, dict) and r.get("git_repo")
        ]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "repo_list.json")
            self.log_results(
                tmp_path,
                artifact_path="repos",
                tags=repo_tags,
                content=json.dumps(public_repos),
            )

    def load_run_repos(self, run_id: str | None = None) -> list:
        """Loads the repo list from MLflow, scoped by run_id if set, falling back to GIT_REPO_LIST_CONTENTS."""
        from utils.pipeline_utils import get_pipeline_run_id

        asset_tags: dict = {"category": "repositories"}
        pipeline_run_id = run_id or get_pipeline_run_id()
        if pipeline_run_id:
            asset_tags["pipeline_run_id"] = pipeline_run_id

        try:
            uris = self._get_absolute_artifact_uris(
                "repos/repo_list.json",
                experiment_name=self.RESULT_ASSET_EXPERIMENT,
                tags=asset_tags,
            )
            repos = self._download_and_merge_enrichments(uris)
            if repos is not None:
                return repos
        except Exception:
            logging.debug("load_run_repos: no pipeline-scoped repo list found", exc_info=True)

        return json.loads(os.getenv("GIT_REPO_LIST_CONTENTS", "[]") or "[]")

    def load_catalog_repos(self) -> list:
        """Downloads the full repo catalog from MLflow, trying enrichments first and falling back to static assets."""
        repos = self.download("repos/repo_list.json")
        if repos is None:
            repos = self.download("repos/repo_list1.json")
        if repos is not None:
            return repos
        from .local_asset_loader import LocalAssetLoader

        return LocalAssetLoader(
            namespace=getattr(self, "namespace", None),
            workspace=getattr(self, "workspace", None),
        ).load_catalog_repos()

    def delete_repo(self, git_repo: str, git_branch: str = "main") -> list:
        """Removes the matching repository from repos/repo_list.json in MLflow enrichments."""
        with _REPO_CATALOG_LOCK:
            base = self.load_catalog_repos() or []
            remaining = _remove_from_repo_catalog(base, git_repo, git_branch)
            self.register_enrichments(enrichment_files={"repos/repo_list.json": remaining})
            return remaining

    def _resolve_purge_experiments(self, client) -> tuple[list[str], str | None]:
        from utils.pipeline_utils import resolve_mlflow_prefix

        prefix = resolve_mlflow_prefix(namespace=self.namespace, workspace=self.workspace)
        eval_exp = f"{prefix}/code-refactoring/evaluations"
        ns_ws = (self.namespace or os.environ.get("MLFLOW_WORKSPACE") or "").strip()
        telemetry_exp = os.environ.get("MLFLOW_EXPERIMENT_NAME") or (
            f"{ns_ws}/AIP-default" if ns_ws else "AIP-default"
        )
        exp_names = list(
            dict.fromkeys(
                [
                    self.RESULT_DIRECTORY_ASSET_EXPERIMENT,
                    self.RESULT_ASSET_EXPERIMENT,
                    eval_exp,
                    telemetry_exp,
                ]
            )
        )
        active_exp_ids: list[str] = []
        telemetry_exp_id: str | None = None
        for exp_name in exp_names:
            exp = client.get_experiment_by_name(exp_name)
            if not exp or getattr(exp, "lifecycle_stage", None) != "active":
                continue
            exp_id = str(exp.experiment_id)
            active_exp_ids.append(exp_id)
            if exp_name == telemetry_exp:
                telemetry_exp_id = exp_id
        return active_exp_ids, telemetry_exp_id

    @staticmethod
    def _delete_traces_for_kfp_runs(
        client,
        telemetry_exp_id: str | None,
        clean_run_ids: list[str],
    ) -> None:
        if not (
            telemetry_exp_id
            and clean_run_ids
            and hasattr(client, "search_traces")
            and hasattr(client, "delete_traces")
        ):
            return
        for rid in clean_run_ids:
            safe_rid = escape_mlflow_filter_value(rid)
            try:
                trace_ids: list[str] = []
                page_token: str | None = None
                seen_tokens: set[str] = set()
                while True:
                    trace_kwargs: dict[str, object] = {
                        "locations": [telemetry_exp_id],
                        "filter_string": f"tags.`kfp.run_id` = '{safe_rid}'",
                        "include_spans": False,
                    }
                    if page_token:
                        trace_kwargs["page_token"] = page_token
                    traces = client.search_traces(**trace_kwargs)
                    trace_ids.extend(
                        tid
                        for t in (traces or [])
                        if isinstance(
                            (
                                tid := getattr(getattr(t, "info", None), "trace_id", None)
                                or getattr(getattr(t, "info", None), "request_id", None)
                            ),
                            str,
                        )
                        and tid
                    )
                    next_token = getattr(traces, "token", None)
                    if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                        break
                    seen_tokens.add(next_token)
                    page_token = next_token
                for i in range(0, len(trace_ids), 100):
                    client.delete_traces(
                        experiment_id=telemetry_exp_id,
                        trace_ids=trace_ids[i : i + 100],
                    )
            except Exception:
                logging.debug(
                    "Skipping trace cleanup in experiment %s for run %s",
                    telemetry_exp_id,
                    rid,
                    exc_info=True,
                )

    def purge_repo_records(
        self,
        git_repo: str,
        git_branch: str = "main",
        kfp_run_ids: list[str] | None = None,
    ) -> int:
        """Soft-deletes single-repo MLflow runs and traces for ``(git_repo, git_branch)``."""
        git_slug = _repo_slug(git_repo, git_branch)
        if not git_slug:
            return 0

        safe_slug = escape_mlflow_filter_value(git_slug)
        filters = [f"tags.\"git_slug\" = '{safe_slug}'"]
        clean_run_ids = list(
            dict.fromkeys(r.strip() for r in (kfp_run_ids or []) if isinstance(r, str) and r.strip())
        )
        for rid in clean_run_ids:
            safe_rid = escape_mlflow_filter_value(rid)
            filters.append(f"tags.\"pipeline_run_id\" = '{safe_rid}'")
            filters.append(f"tags.\"kfp_run_id\" = '{safe_rid}'")

        client = MlflowClient()
        active_exp_ids, telemetry_exp_id = self._resolve_purge_experiments(client)
        if not active_exp_ids:
            return 0

        matched_runs: dict[str, object] = {}
        for flt in filters:
            for run in _search_all_runs(client, active_exp_ids, flt):
                rid = getattr(getattr(run, "info", None), "run_id", None)
                if isinstance(rid, str) and rid:
                    matched_runs[rid] = run

        deleted_count = 0
        for run_id, run in matched_runs.items():
            tags = _run_tags(run)
            if str(tags.get("multi_repo", "")).strip().lower() in ("true", "1", "yes"):
                continue
            slug_tag = str(tags.get("git_slug") or "").strip()
            if tags.get("category") == "enrichments" and not slug_tag:
                continue
            if slug_tag and slug_tag != git_slug:
                continue
            client.delete_run(run_id)
            deleted_count += 1

        self._delete_traces_for_kfp_runs(client, telemetry_exp_id, clean_run_ids)
        return deleted_count

    def purge_run_records(
        self,
        run_id: str,
        git_repo: str = "",
        git_branch: str = "main",
        multi_repo: bool = False,
        project_slug: str = "",
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        purge_untagged_for_target: bool = False,
    ) -> int:
        """Soft-deletes MLflow runs and traces associated with a single KFP pipeline run."""
        from utils.pipeline_utils import sanitize_slug

        clean_id = (run_id or "").strip()
        if not clean_id:
            return 0

        clean_project_slug = sanitize_slug(project_slug)
        git_slug = (
            (clean_project_slug or "multi-repo")
            if multi_repo
            else (_repo_slug(git_repo, git_branch) if git_repo else clean_project_slug)
        )

        client = MlflowClient()
        active_exp_ids, telemetry_exp_id = self._resolve_purge_experiments(client)
        if not active_exp_ids:
            return 0

        safe_id = escape_mlflow_filter_value(clean_id)
        filters = [
            f"tags.\"pipeline_run_id\" = '{safe_id}'",
            f"tags.\"kfp_run_id\" = '{safe_id}'",
        ]
        if git_slug and (purge_untagged_for_target or start_time_ms is not None):
            safe_slug = escape_mlflow_filter_value(git_slug)
            filters.append(f"tags.\"git_slug\" = '{safe_slug}'")

        matched_runs: dict[str, object] = {}
        for flt in filters:
            for run in _search_all_runs(client, active_exp_ids, flt):
                rid = getattr(getattr(run, "info", None), "run_id", None)
                if isinstance(rid, str) and rid:
                    matched_runs[rid] = run

        win_start = (start_time_ms - 5_000) if start_time_ms is not None else None
        win_end = (
            ((end_time_ms if end_time_ms is not None else start_time_ms) + 60_000)
            if start_time_ms is not None
            else None
        )

        deleted_count = 0
        for mlf_run_id, run in matched_runs.items():
            tags = _run_tags(run)
            slug_tag = str(tags.get("git_slug") or "").strip()
            if tags.get("category") == "enrichments" and not slug_tag:
                continue

            p_rid = str(tags.get("pipeline_run_id") or "").strip()
            k_rid = str(tags.get("kfp_run_id") or "").strip()
            if p_rid == clean_id or k_rid == clean_id:
                client.delete_run(mlf_run_id)
                deleted_count += 1
                continue
            if p_rid or k_rid:
                continue

            if not git_slug or slug_tag != git_slug:
                continue
            if "multi_repo" in tags:
                run_is_multi = str(tags.get("multi_repo") or "").strip().lower() in (
                    "true",
                    "1",
                    "yes",
                )
                if run_is_multi != bool(multi_repo):
                    continue
            if not purge_untagged_for_target:
                if str(tags.get("adhoc_query") or "").strip().lower() == "true":
                    continue
                run_start = getattr(getattr(run, "info", None), "start_time", None)
                if (
                    win_start is None
                    or win_end is None
                    or not isinstance(run_start, (int, float))
                    or not (win_start <= run_start <= win_end)
                ):
                    continue

            client.delete_run(mlf_run_id)
            deleted_count += 1

        self._delete_traces_for_kfp_runs(client, telemetry_exp_id, [clean_id])
        return deleted_count

    def num_prompts(self, prompt_prefix: str) -> int:
        """Returns the number of prompts in the MLflow registry matching the given prefix."""
        try:
            prefix = prompt_prefix.replace("/", "-")
            prompts = self._prompt_api().search_prompts(filter_string=f"name LIKE '{prefix}--%'")
            return len(prompts)
        except Exception as e:
            logging.error(f"Error counting prompts with prefix {prompt_prefix}: {e}")
            return 0

    def upload_all_assets(self, assets_dir: str):
        """Uploads all assets from a directory to the static MLflow experiment in a single run."""
        try:
            client = MlflowClient()
            experiment = self.get_or_create_experiment_by_name(client, self.STATIC_ASSET_EXPERIMENT)
            run = client.create_run(experiment.experiment_id, run_name=self._RUN_NAME)
            run_id = run.info.run_id
            logging.info(f"Created run: {run_id}")

            try:
                for entry in sorted(os.scandir(assets_dir), key=lambda e: e.name):
                    if entry.is_dir():
                        logging.info(f"Uploading {entry.name}/")
                        client.log_artifacts(run_id, entry.path, artifact_path=entry.name)
                    elif entry.is_file():
                        logging.info(f"Uploading {entry.name}")
                        client.log_artifact(run_id, entry.path, artifact_path="")

                for root, _, files in os.walk(self._PROMPTS_DIR):
                    for file in sorted(files):
                        abs_path = os.path.join(root, file)
                        rel_path = os.path.relpath(abs_path, self._PROMPTS_DIR)
                        prompt_path = os.path.splitext(rel_path)[0]
                        logging.info(f"Registering prompt '{prompt_path}'")
                        self.upload_prompt(prompt_path)

                client.set_terminated(run_id, status="FINISHED")
                logging.info(f"Upload complete. Run id: {run_id}")
                self._mark_as_latest(client, experiment.experiment_id, run_id)
            except Exception:
                client.set_terminated(run_id, status="FAILED")
                raise
        except Exception as e:
            logging.error(f"Error uploading assets from {assets_dir}: {e}")
            raise e
