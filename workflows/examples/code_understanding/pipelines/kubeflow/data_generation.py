import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))

from kfp import dsl
from kfp.dsl import Dataset, Input, Output
from utils.kubeflow_utils import DATA_GENERATION_BASE_IMAGE, get_pip_installable_git_url, inject_secret_as_env

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
@dsl.component(base_image=DATA_GENERATION_BASE_IMAGE, packages_to_install=[_AGENTMESH_INSTALLABLE_URL])
def benchmark_llm_op(
    override_concurrency: int = 0,
    git_repo: str = "",
    git_branch: str = "main",
    multi_repo: bool = False,
) -> int:
    """Benchmarks the configured LLM endpoint using GuideLLM to find the best concurrency setting."""
    from contextlib import nullcontext
    from pipelines.base.data_generation import generate_git_slug
    from utils.kubeflow_utils import setup_logging
    from utils.llm_benchmark import apply_best_concurrency, run_guidellm_benchmark
    setup_logging()
    import logging

    git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None
    dur_tracker = None
    try:
        from utils.duration_tracker import DurationTracker
        DurationTracker.reset_instance()
        dur_tracker = DurationTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
    except Exception as e:
        logging.debug(f"DurationTracker initialization skipped in benchmark_llm_op: {e}")

    if override_concurrency and int(override_concurrency) > 0:
        best = apply_best_concurrency(int(override_concurrency))
        logging.info(f"Using pre-computed concurrency override: {best}")
        if dur_tracker:
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            except Exception:
                pass
            has_bench = any(
                "guidellm" in str(r.get("step", "")).lower()
                or "concurrency benchmark" in str(r.get("step", "")).lower()
                for r in dur_tracker.records
            )
            if not has_bench:
                dur_tracker.record_step(
                    stage="Data Generation",
                    step="GuideLLM Concurrency Benchmark",
                    duration=0.0,
                    status="success",
                    metadata={"best_concurrency": int(best), "runner": "override", "skipped": True},
                )
        return int(best)

    cm_bench = (
        dur_tracker.measure(stage="Data Generation", step="GuideLLM Concurrency Benchmark")
        if dur_tracker
        else nullcontext()
    )
    with cm_bench:
        result = run_guidellm_benchmark(apply_env=True)

    best_concurrency = int(result.get("best_concurrency", 10))
    if dur_tracker:
        for rec in reversed(dur_tracker.records):
            if rec.get("step") == "GuideLLM Concurrency Benchmark":
                meta = rec.setdefault("metadata", {})
                meta["best_concurrency"] = best_concurrency
                meta["runner"] = result.get("runner")
                meta["skipped"] = bool(result.get("skipped", False))
                if rec.get("duration", 0.0) <= 0.001 and float(result.get("duration_seconds", 0.0) or 0.0) > 0.0:
                    rec["duration"] = float(result["duration_seconds"])
                break
        try:
            dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Pre-Pipeline", multi_repo=multi_repo)
            dur_tracker.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
        except Exception as e:
            logging.debug(f"Failed to upload benchmark duration to MLflow in benchmark_llm_op: {e}")
        try:
            logging.info("\n" + dur_tracker.format_summary())
        except Exception as e:
            logging.debug(f"Failed to print duration summary in benchmark_llm_op: {e}")

    logging.info(f"GuideLLM benchmark selected best_concurrency={best_concurrency}")
    return best_concurrency


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
            dur_tracker = DurationTracker.get_instance()
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, current_stage="Data Generation")
            except Exception as e:
                logging.debug(f"Prior benchmark duration download skipped in prepare_environment_op: {e}")
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
            code_tracker = CodeMetricsTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
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
    multi_repo: bool = False,
    max_concurrency: int = 0):
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

    if max_concurrency and int(max_concurrency) > 0:
        try:
            from utils.llm_benchmark import apply_best_concurrency
            apply_best_concurrency(int(max_concurrency))
        except Exception as e:
            logging.debug(f"Failed to apply max_concurrency in generate_code_and_meta_op: {e}")

    with read_from_input_artifact(source_dir) as tmp_source, write_to_output_artifact(target_dir) as tmp_target:
        if max_concurrency and int(max_concurrency) > 0:
            try:
                from utils.llm_benchmark import save_benchmark_result
                save_benchmark_result(
                    {"best_concurrency": int(max_concurrency), "runner": "kubeflow_benchmark_llm_op"},
                    tmp_target,
                )
            except Exception as e:
                logging.debug(f"Failed to persist benchmark_results.json in generate_code_and_meta_op: {e}")
        git_slug = generate_git_slug(git_repo, git_branch) if git_repo else None
        ignore_policy = RepositoryIgnorePolicy.from_repository(tmp_source)

        try:
            import shutil
            from provenance.pipeline import SIDE_CAR as PROVENANCE_SIDECAR, capture as capture_provenance
            prov_src = os.path.join(tmp_source, PROVENANCE_SIDECAR)
            prov_dst = os.path.join(tmp_target, PROVENANCE_SIDECAR)
            if os.path.exists(prov_src):
                shutil.copy2(prov_src, prov_dst)
            else:
                capture_provenance(tmp_source, tmp_target, run_syft=False)
        except Exception as e:
            logging.debug(f"Provenance sidecar propagation in generate_code_and_meta_op: {e}")

        dur_tracker = None
        try:
            from utils.duration_tracker import DurationTracker, extract_benchmark_duration
            dur_tracker = DurationTracker.get_instance()
            try:
                dur_tracker.download_from_mlflow(git_slug=git_slug, multi_repo=multi_repo)
            except Exception as e:
                logging.debug(f"MLflow download durations skipped in generate_code_and_meta_op: {e}")
            dur_file = os.path.join(tmp_source, "durations.json")
            if os.path.exists(dur_file):
                dur_tracker.load_and_merge(dur_file)
            extract_benchmark_duration([tmp_source, tmp_target], dur_tracker)
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
            code_tracker = CodeMetricsTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
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
                    code_tracker.record_ignored_locations(ignore_policy)
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
    max_concurrency: int = 0,
) -> Dataset:

    bench = benchmark_llm_op(
        override_concurrency=max_concurrency,
        git_repo=git_repo,
        git_branch=git_branch,
        multi_repo=multi_repo,
    )

    prep = prepare_environment_op(
        git_repo=git_repo,
        git_branch=git_branch,
        git_username=git_username,
        git_token=git_token
    )
    prep.after(bench)

    gen = generate_code_and_meta_op(
        git_repo=git_repo,
        git_branch=git_branch,
        source_dir=prep.outputs["source_dir"],
        multi_repo=multi_repo,
        max_concurrency=bench.output,
    )

    return gen.outputs["target_dir"]

@dsl.pipeline(name="data-generation-multi-repo-pipeline")
def _run_pipeline_multi_repo():
    """Generates code metadata for all repositories in the asset-loader repo list."""

    bench = benchmark_llm_op(multi_repo=True)
    repo_list_task = get_repo_list_op()
    repo_list_task.after(bench)

    with dsl.ParallelFor(items=repo_list_task.output,
                         parallelism=int(os.getenv("GRAPHRAG_PARALLEL_REPOS", "2"))) as repo:

        _run_pipeline(
            git_repo=repo.git_repo,
            git_branch=repo.git_branch,
            git_username=repo.git_username,
            git_token=repo.git_token,
            multi_repo=True,
            max_concurrency=bench.output,
        )


##############################################################################
# Pipeline stage
##############################################################################

class DataGenerationPipeline:
    run = staticmethod(_run_pipeline)
    run_multi_repo = staticmethod(_run_pipeline_multi_repo)
