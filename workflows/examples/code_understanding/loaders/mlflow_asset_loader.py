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
import requests

logging.basicConfig(level=os.environ.get('LOGLEVEL', 'INFO').upper())

from .asset_loader import AssetLoader
from utils.request_timing import timing_span, timed, timing_enabled
from utils.artifact_measurement import measure_payload, upload_identity

_SESSION_PATCHED = False


def _is_mlflow_tracking_request(request_url: str | None) -> bool:
    """Return True only when the request targets the configured MLFLOW_TRACKING_URI."""
    tracking_uri = (os.environ.get("MLFLOW_TRACKING_URI") or "").strip()
    if not tracking_uri or not tracking_uri.startswith(("http://", "https://")):
        return True
    if not request_url:
        return False
    from urllib.parse import urlparse

    req_parsed = urlparse(str(request_url))
    trk_parsed = urlparse(tracking_uri)
    if not req_parsed.netloc or not trk_parsed.netloc:
        return False
    return req_parsed.netloc.lower() == trk_parsed.netloc.lower()


def _patch_session_forwarded_token():
    """Patches requests.Session.send once to attach X-Forwarded-Access-Token for MLflow requests."""
    global _SESSION_PATCHED
    if _SESSION_PATCHED:
        return
    _orig_send = requests.Session.send

    def _send_with_forwarded_token(self, request, **kwargs):
        token = os.environ.get("MLFLOW_TRACKING_TOKEN")
        if token and _is_mlflow_tracking_request(getattr(request, "url", None)):
            request.headers["X-Forwarded-Access-Token"] = token
        return _orig_send(self, request, **kwargs)

    requests.Session.send = _send_with_forwarded_token
    _SESSION_PATCHED = True


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

    def _get_global_enrichment_runs(self, client, experiment_id: str, base_filter: str) -> list:
        """Returns the ordered list of global enrichment runs to merge: [checkpoint] + [deltas].

        The checkpoint run (tagged latest=true) is first; delta runs (no latest tag, created
        after the checkpoint) follow in chronological order. Only unarchived runs are returned.
        """
        archived_filter = f"{base_filter} AND tags.archived IS NULL" if base_filter else "tags.archived IS NULL"
        checkpoint_runs = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=f"{archived_filter} AND tags.latest = 'true'",
            order_by=["attributes.start_time DESC"],
            max_results=1,
        )
        checkpoint = checkpoint_runs[0] if checkpoint_runs else None

        delta_filter = f"{archived_filter} AND tags.latest IS NULL"
        if checkpoint:
            delta_filter += f" AND attributes.start_time >= {checkpoint.info.start_time}"

        delta_runs = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=delta_filter,
            order_by=["attributes.start_time ASC"],
        )

        return ([checkpoint] if checkpoint else []) + list(delta_runs)

    def _mark_as_archived(self, client, anchor_run) -> None:
        """Archives all unarchived global enrichment runs created before anchor_run."""
        stale = client.search_runs(
            experiment_ids=[anchor_run.info.experiment_id],
            filter_string=(
                "tags.category = 'enrichments'"
                ' AND tags."git_slug" IS NULL'
                " AND tags.archived IS NULL"
                f" AND attributes.start_time < {anchor_run.info.start_time}"
            ),
        )
        for stale_run in stale:
            client.set_tag(stale_run.info.run_id, "archived", "true")

    @staticmethod
    def _has_prior_enrichment_checkpoint(runs: list) -> bool:
        """Return True if any run in the list is a checkpoint (tagged latest=true)."""
        return any(
            (getattr(getattr(r, "data", None), "tags", None) or {}).get("latest") == "true"
            for r in runs
        )

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
        for k, v in (tags or {}).items():
            if v is None:
                tag_filter_parts.append(f'tags."{k}" IS NULL')
            elif v != "":
                tag_filter_parts.append(f"tags.\"{k}\" = '{str(v)}'")

        if is_global_scoped_enrichment_tagset(tags):
            enrichment_experiment = self.get_or_create_experiment_by_name(
                client, self.RESULT_DIRECTORY_ASSET_EXPERIMENT
            )
            base_filter = " AND ".join(tag_filter_parts)
            runs = self._get_global_enrichment_runs(
                client, enrichment_experiment.experiment_id, base_filter
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
        if tags:
            for k, v in tags.items():
                if v is None:
                    filter_parts.append(f'tags."{k}" IS NULL')
                elif v != "":
                    filter_parts.append(f"tags.\"{k}\" = '{str(v)}'")

        existing = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=" AND ".join(filter_parts),
        )
        for run in existing:
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
        else:
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
                with timing_span("mlflow.artifact_download", "io"):
                    local_path = mlflow.artifacts.download_artifacts(artifact_uri=asset_uris[0])

                os.makedirs(download_dir, exist_ok=True)
                with timing_span("mlflow.artifact_copy", "io"):
                    shutil.copytree(local_path, download_dir, dirs_exist_ok=True)

                enrichment_prefix = os.path.join(".enrichments", ".assets")
                for root, _, files in os.walk(download_dir):
                    for f in files:
                        full_path = os.path.join(root, f)
                        if enrichment_prefix not in full_path:
                            continue
                        rel_path = os.path.relpath(full_path, download_dir)
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

            client = MlflowClient()
            if os.path.exists(assets_dir):
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
        from utils.loader_utils import merge_json_enrichment
        from utils.pipeline_utils import get_pipeline_run_id

        pipeline_run_id = get_pipeline_run_id(allow_generate=True)

        try:
            base = self.download("repos/repo_list.json") or []
            catalog_repos = merge_json_enrichment(base, run_repos)
            self.register_enrichments(enrichment_files={"repos/repo_list.json": catalog_repos})
        except Exception:
            logging.warning("Failed to update repo catalog enrichment", exc_info=True)

        repo_tags = {"category": "repositories"}
        if pipeline_run_id:
            repo_tags["pipeline_run_id"] = pipeline_run_id

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "repo_list.json")
            self.log_results(
                tmp_path,
                artifact_path="repos",
                tags=repo_tags,
                content=json.dumps(run_repos),
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
        repos = self.download("repos/repo_list.json") or self.download("repos/repo_list1.json")
        if repos:
            return repos
        from .local_asset_loader import LocalAssetLoader

        return LocalAssetLoader(
            namespace=getattr(self, "namespace", None),
            workspace=getattr(self, "workspace", None),
        ).load_catalog_repos()

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
