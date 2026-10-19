import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Output
from utils.kubeflow_utils import (
    DATA_GENERATION_BASE_IMAGE,
    PIPELINE_TOOLS_BASE_IMAGE,
    get_pip_installable_git_url,
    inject_secret_as_env,
)

_AGENTMESH_INSTALLABLE_URL = get_pip_installable_git_url(
    git_username=os.getenv("AUGUR_GIT_REPO_USERNAME"),
    git_token=os.getenv("AUGUR_GIT_REPO_TOKEN"),
    repo_url=os.getenv("AUGUR_GIT_REPO_URL", ""),
    repo_ref=os.getenv("AUGUR_GIT_REPO_BRANCH", "main"),
    subdirectory="workflows/examples/code_understanding",
)

##############################################################################
# Components
##############################################################################

@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=PIPELINE_TOOLS_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def guidellm_calibration_op(
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
    enabled: bool = False,
) -> int:
    """Runs a GuideLLM concurrency calibration sweep before Stage 1 when enabled
    and uploads benchmarks.json to MLflow for downstream auto-tuning."""
    from pipelines.base.data_generation import generate_git_slug
    from utils.kubeflow_utils import setup_logging
    setup_logging()
    import logging, os

    from utils.benchmark_tracker import BenchmarkTracker, _clean_env

    git_repo = git_repo or _clean_env("GIT_REPO", "")
    git_branch = git_branch or _clean_env("GIT_BRANCH", "main")
    git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

    should_run = enabled or _clean_env("GUIDELLM_CALIBRATE_PIPELINE", "false").lower() in ("true", "1", "yes")

    try:
        bt = BenchmarkTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
        bt.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)

        if should_run:
            from contextlib import nullcontext
            dur_tracker = None
            try:
                from utils.duration_tracker import DurationTracker
                DurationTracker.reset_instance()
                dur_tracker = DurationTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
                dur_tracker.record_pod_creation_overhead(stage="Calibration")
            except Exception as e:
                logging.debug(f"DurationTracker initialization skipped in guidellm_calibration_op: {e}")

            profile_arg = _clean_env("GUIDELLM_PROFILE", "sdg").lower()
            max_sec = int(_clean_env("GUIDELLM_MAX_SECONDS", "15"))
            rates_str = _clean_env("GUIDELLM_RATES", _clean_env("GUIDELLM_CONCURRENCY_RATES", "1,4,8,16"))
            rates = [int(r.strip()) for r in rates_str.split(",") if r.strip().isdigit()] or [1, 4, 8, 16]

            logging.info(
                f"Running GuideLLM pre-stage calibration sweep (profile={profile_arg}, "
                f"rates={rates}, max_seconds={max_sec})..."
            )
            cal_cm = (
                dur_tracker.measure(stage="Calibration", step="guidellm-calibration-op")
                if dur_tracker
                else nullcontext()
            )
            with cal_cm:
                profiles_to_run = ["sdg", "indexing"] if profile_arg == "all" else [profile_arg]
                for prof in profiles_to_run:
                    bt.run_guidellm_profile(profile_name=prof, rates=rates, max_seconds=max_sec)

                bt.log_to_mlflow()
                bt.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                logging.info("\n" + bt.format_summary())

            if dur_tracker:
                try:
                    dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Calibration", multi_repo=multi_repo)
                    logging.info("\n" + dur_tracker.format_summary())
                except Exception as e:
                    logging.debug(f"Failed to upload Calibration durations to MLflow: {e}")
        else:
            logging.info("GuideLLM calibration step skipped (enabled=False). Using existing MLflow benchmark data if present.")
            try:
                from utils.duration_tracker import DurationTracker
                DurationTracker.reset_instance().clear_mlflow_stage(
                    git_slug=git_slug, stage="Calibration", multi_repo=multi_repo
                )
            except Exception as e:
                logging.debug(f"Failed to clear Calibration stage durations in MLflow: {e}")

        rec = bt.get_recommended_sdg_concurrency(default=int(_clean_env("SDG_LLM_MAX_CONCURRENCY", "10")))
        logging.info(f"GuideLLM recommended SDG_LLM_MAX_CONCURRENCY={rec}")
        return int(rec)
    except Exception as e:
        logging.warning(f"GuideLLM calibration encountered an error (falling back to default concurrency): {e}")
        return int(_clean_env("SDG_LLM_MAX_CONCURRENCY", "10"))


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def prepare_environment_op(git_repo: str, 
    git_branch: str, 
    git_username: str, 
    git_token: str, 
    source_dir: Output[Dataset]):
    """Clones the repository and archives it as a gzip tarball."""

    from pipelines.base.data_generation import prepare_environment, generate_git_slug
    from utils.kubeflow_utils import setup_logging, write_to_output_artifact, use_ephemeral_space
    setup_logging()
    import logging, os

    with write_to_output_artifact(source_dir) as tmp_source, use_ephemeral_space() as tmp_target:
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker
            DurationTracker.reset_instance()
            dur_tracker = DurationTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
            dur_tracker.download_from_mlflow(git_slug=git_slug, current_stage="Data Generation")
        except Exception as e:
            logging.debug(f"DurationTracker initialization skipped in prepare_environment_op: {e}")

        prepare_environment(
            source_path=tmp_source,
            target_path=tmp_target,
            git_repo=git_repo,
            git_branch=git_branch,
            git_username=git_username,
            git_token=git_token,
        )

        if dur_tracker:
            try:
                dur_tracker.save_to_file(os.path.join(tmp_source, "durations.json"))
            except Exception as e:
                logging.debug(f"Failed to save durations.json in prepare_environment_op: {e}")
            try:
                dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation")
            except Exception as e:
                logging.debug(f"Failed to upload durations to MLflow in prepare_environment_op: {e}")

        try:
            from utils.token_tracker import TokenCostTracker
            TokenCostTracker.reset_instance()
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.save_to_file(os.path.join(tmp_source, "tokens.json"))
            token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation")
        except Exception as e:
            logging.debug(f"TokenCostTracker handling in prepare_environment_op: {e}")

        try:
            from utils.code_metrics_tracker import CodeMetricsTracker
            CodeMetricsTracker.reset_instance()
            code_tracker = CodeMetricsTracker.get_instance(git_slug=git_slug)
            code_tracker.measure_repository(tmp_source)
            code_tracker.save_to_file(os.path.join(tmp_source, "project_metrics.json"))
            code_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation")
        except Exception as e:
            logging.debug(f"CodeMetricsTracker handling in prepare_environment_op: {e}")


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def generate_code_and_meta_op(
    git_repo: str, 
    git_branch: str,
    source_dir: Input[Dataset], 
    target_dir: Output[Dataset],
    multi_repo: bool = False):
    """Detects languages and generates code metadata for all detected languages."""

    from pipelines.base.data_generation import (
        detect_languages, generate_code_and_meta, generate_git_slug,
        should_reraise_processing_error,
    )
    from utils.repository_ignore import RepositoryIgnorePolicy
    from utils.kubeflow_utils import setup_logging, read_from_input_artifact, write_to_output_artifact
    setup_logging()

    import logging, os
    from contextlib import nullcontext

    with read_from_input_artifact(source_dir) as tmp_source, write_to_output_artifact(target_dir) as tmp_target:
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None
        ignore_policy = RepositoryIgnorePolicy.from_repository(tmp_source)

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance()
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"MLflow download durations skipped in generate_code_and_meta_op: {e}")
            dur_file = os.path.join(tmp_source, "durations.json")
            if os.path.exists(dur_file):
                dur_tracker.load_and_merge(dur_file)
        except Exception as e:
            logging.debug(f"DurationTracker handling in generate_code_and_meta_op: {e}")

        token_tracker = None
        try:
            from utils.token_tracker import TokenCostTracker
            TokenCostTracker.reset_instance()
            token_tracker = TokenCostTracker.get_instance()
            token_tracker.enable_litellm_callbacks(category="Data Generation")
            token_tracker.enable_openai_tracking(category="Data Generation")
            tokens_file = os.path.join(tmp_source, "tokens.json")
            if os.path.exists(tokens_file):
                token_tracker.merge(TokenCostTracker.load_from_file(tokens_file))
        except Exception as e:
            logging.debug(f"TokenCostTracker handling in generate_code_and_meta_op: {e}")

        code_tracker = None
        try:
            from utils.code_metrics_tracker import CodeMetricsTracker
            CodeMetricsTracker.reset_instance()
            code_tracker = CodeMetricsTracker.get_instance(git_slug=git_slug)
            try:
                code_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"MLflow download code metrics skipped in generate_code_and_meta_op: {e}")
            metrics_file = os.path.join(tmp_source, "project_metrics.json")
            if os.path.exists(metrics_file):
                code_tracker.load_and_merge(metrics_file)
            else:
                code_tracker.measure_repository(tmp_source)
        except Exception as e:
            logging.debug(f"CodeMetricsTracker handling in generate_code_and_meta_op: {e}")

        bench_tracker = None
        try:
            from utils.benchmark_tracker import BenchmarkTracker
            bench_tracker = BenchmarkTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
            bench_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            bench_file = os.path.join(tmp_source, "benchmarks.json")
            if os.path.exists(bench_file):
                bench_tracker.load_and_merge(bench_file)
            if bench_tracker.profiles and code_tracker and code_tracker.total_repo_files > 0:
                est = bench_tracker.estimate_pipeline_runtime(code_tracker.total_repo_files)
                logging.info(
                    f"GuideLLM Estimated Pipeline Wall-Clock for {code_tracker.total_repo_files} files: "
                    f"~{est['total_minutes']} min (Stage 1: {est['stage1_sdg_seconds']}s, "
                    f"Stage 2: {est['stage2_indexing_seconds']}s, Stage 3: {est['stage3_analysis_seconds']}s)"
                )
        except Exception as e:
            logging.debug(f"BenchmarkTracker handling in generate_code_and_meta_op: {e}")

        try:
            from pipelines.base.data_generation import load_external_data

            # Immediately write initial telemetry files to tmp_target
            if dur_tracker:
                try:
                    dur_tracker.save_to_file(os.path.join(tmp_target, "durations.json"))
                except Exception:
                    pass
            if token_tracker:
                try:
                    token_tracker.save_to_file(os.path.join(tmp_target, "tokens.json"))
                except Exception:
                    pass
            if code_tracker:
                try:
                    code_tracker.save_to_file(os.path.join(tmp_target, "project_metrics.json"))
                except Exception:
                    pass
            if bench_tracker and bench_tracker.profiles:
                try:
                    bench_tracker.save_to_file(os.path.join(tmp_target, "benchmarks.json"))
                except Exception:
                    pass

            cm_load = dur_tracker.measure(stage="Data Generation", step="Load External Data") if dur_tracker else nullcontext()
            with cm_load:
                external_metadata = load_external_data(tmp_source, ignore_policy)

            cm_detect = dur_tracker.measure(stage="Data Generation", step="Detect Languages") if dur_tracker else nullcontext()
            with cm_detect:
                languages = detect_languages(tmp_source, ignore_policy)

            for language in languages:
                for config in [False, True]:
                    generate_code_and_meta(
                        git_repo=git_repo, git_branch=git_branch,
                        language=language, source_path=tmp_source, target_path=tmp_target,
                        config=config, multi_repo=multi_repo,
                        external_metadata=external_metadata,
                        ignore_policy=ignore_policy,
                    )
        except Exception as e:
            if type(e).__name__ == "RateLimitError" or "429" in str(e):
                logging.error(
                    f"Rate limit exceeded for repo '{git_repo}' (branch='{git_branch}'). "
                    f"Consider reducing GRAPHRAG_PARALLEL_REPOS: {e}"
                )
                raise
            logging.error(
                f"Error processing repo '{git_repo}' (branch='{git_branch}'): {e}"
            )
            if should_reraise_processing_error(e, multi_repo=multi_repo):
                raise
        else:
            ignore_policy.log_summary()
            has_txt = any(f.endswith(".txt") for _, _, files in os.walk(tmp_target) for f in files)
            if not has_txt:
                err_msg = f"No text or code files were generated in target directory for repo '{git_repo}'."
                logging.error(err_msg)
                if not multi_repo:
                    raise RuntimeError(err_msg)
        finally:
            if dur_tracker:
                try:
                    dur_tracker.save_to_file(os.path.join(tmp_target, "durations.json"))
                except Exception as e:
                    logging.debug(f"Failed to save durations.json in generate_code_and_meta_op: {e}")
                try:
                    dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload durations to MLflow in generate_code_and_meta_op: {e}")
                try:
                    summary = dur_tracker.format_summary()
                    logging.info("\n" + summary)
                except Exception as e:
                    logging.debug(f"Failed to print duration summary in data generation pod: {e}")

            if token_tracker:
                try:
                    token_tracker.save_to_file(os.path.join(tmp_target, "tokens.json"))
                except Exception as e:
                    logging.debug(f"Failed to save tokens.json in generate_code_and_meta_op: {e}")
                try:
                    token_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload tokens to MLflow in generate_code_and_meta_op: {e}")
                try:
                    summary = token_tracker.format_summary()
                    logging.info("\n" + summary)
                except Exception as e:
                    logging.debug(f"Failed to print token summary in data generation pod: {e}")

            if code_tracker:
                try:
                    code_tracker.save_to_file(os.path.join(tmp_target, "project_metrics.json"))
                except Exception as e:
                    logging.debug(f"Failed to save project_metrics.json in generate_code_and_meta_op: {e}")
                try:
                    code_tracker.log_to_mlflow()
                    code_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                except Exception as e:
                    logging.debug(f"Failed to upload code metrics to MLflow in generate_code_and_meta_op: {e}")
                try:
                    summary = code_tracker.format_summary()
                    logging.info("\n" + summary)
                except Exception as e:
                    logging.debug(f"Failed to print code metrics summary in data generation pod: {e}")

            if bench_tracker and bench_tracker.profiles:
                try:
                    bench_tracker.save_to_file(os.path.join(tmp_target, "benchmarks.json"))
                    bench_tracker.log_to_mlflow()
                    bench_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
                    logging.info("\n" + bench_tracker.format_summary())
                except Exception as e:
                    logging.debug(f"Failed to persist benchmark metrics in generate_code_and_meta_op: {e}")


@inject_secret_as_env(secret_name="code-understanding-env")
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def get_repo_list_op() -> list:
    """Downloads and returns the repo list from the asset loader."""

    from loaders.default_asset_loader import DefaultAssetLoader
    from utils.kubeflow_utils import setup_logging

    import os
    import json
    
    setup_logging()
    import logging
    logging.info(os.getenv("GIT_REPO_LIST_CONTENTS"))

    repos = json.loads(os.getenv("GIT_REPO_LIST_CONTENTS"))

    # Ensure missing optional fields exist in every item before sending to KFP ParallelFor
    for repo in repos:
        repo.setdefault("git_branch", "main")
        repo.setdefault("git_username", "")
        repo.setdefault("git_token", "")

    return repos


##############################################################################
# Pipelines
##############################################################################

@dsl.pipeline(name="data-generation-pipeline")
def _run_pipeline(
    git_repo: str = os.getenv("GIT_REPO", ""),
    git_branch: str = os.getenv("GIT_BRANCH", "main"),
    git_username: str = os.getenv("GIT_USERNAME", ""),
    git_token: str = os.getenv("GIT_TOKEN", ""),
    multi_repo: bool = False,
) -> Dataset:

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
        git_username=git_username,
        git_token=git_token
    )

    gen = generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_dir=prep.outputs["source_dir"],
        multi_repo=multi_repo,
    )

    return gen.outputs["target_dir"]

@dsl.pipeline(name="data-generation-multi-repo-pipeline")
def _run_pipeline_multi_repo():
    """Generates code metadata for all repositories in the asset-loader repo list."""

    repo_list_task = get_repo_list_op()

    with dsl.ParallelFor(items=repo_list_task.output,
                         parallelism=int(os.getenv("GRAPHRAG_PARALLEL_REPOS", "2"))) as repo:

        _run_pipeline(
            git_repo=repo.git_repo,
            git_branch=repo.git_branch,
            git_username=repo.git_username,
            git_token=repo.git_token,
            multi_repo=True,
        )


##############################################################################
# Pipeline stage
##############################################################################

class DataGenerationPipeline:
    run = staticmethod(_run_pipeline)
    run_multi_repo = staticmethod(_run_pipeline_multi_repo)
    calibrate = staticmethod(guidellm_calibration_op)
