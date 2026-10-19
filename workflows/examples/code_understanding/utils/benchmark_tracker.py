from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union


# AUGUR workload profiles (prompt_tokens, output_tokens) derived from typical stage token distributions
AUGUR_WORKLOAD_PROFILES: Dict[str, Dict[str, Any]] = {
    "precheck": {
        "description": "Fast concurrent pre-flight readiness probe",
        "prompt_tokens": 256,
        "output_tokens": 128,
        "default_profile": "concurrent",
        "default_rates": "5",
        "default_max_seconds": 15,
    },
    "sdg": {
        "description": "Stage 1: SDG Hub code metadata extraction (file -> JSON)",
        "prompt_tokens": 2500,
        "output_tokens": 450,
        "default_profile": "concurrent",
        "default_rates": "1,5,10,20",
        "default_max_seconds": 30,
    },
    "indexing": {
        "description": "Stage 2: GraphRAG entity/relationship extraction & community reports",
        "prompt_tokens": 1200,
        "output_tokens": 600,
        "default_profile": "concurrent",
        "default_rates": "1,5,10,20,25",
        "default_max_seconds": 30,
    },
    "analysis": {
        "description": "Stage 3: Migration report synthesis (map-reduce global/local search)",
        "prompt_tokens": 6000,
        "output_tokens": 1500,
        "default_profile": "concurrent",
        "default_rates": "1,5,10",
        "default_max_seconds": 30,
    },
}

ROLE_ENV_MAP: Dict[str, Tuple[str, str, str, str]] = {
    "graphrag": (
        "GRAPHRAG_LLM_API_BASE",
        "GRAPHRAG_LLM_ID",
        "GRAPHRAG_LLM_API_KEY",
        "GRAPHRAG_LLM_TOKEN",
    ),
    "ground_truth": (
        "GROUND_TRUTH_LLM_API_BASE",
        "GROUND_TRUTH_LLM_ID",
        "GROUND_TRUTH_LLM_API_KEY",
        "GROUND_TRUTH_LLM_TOKEN",
    ),
    "judge": (
        "JUDGE_LLM_API_BASE",
        "JUDGE_LLM_ID",
        "JUDGE_LLM_API_KEY",
        "JUDGE_LLM_TOKEN",
    ),
}


def _clean_env(name: str, default: str = "") -> str:
    """Reads an environment variable, stripping any inline '#' comments and surrounding whitespace."""
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.split("#")[0].strip()
    return cleaned if cleaned != "" else default


def resolve_role_credentials(role: str) -> Tuple[str, str, str]:
    """Resolves (api_base, model_id, api_key) from environment variables for a given role."""
    role_key = role.lower().strip()
    if role_key not in ROLE_ENV_MAP:
        role_key = "graphrag"
    base_var, id_var, key_var, tok_var = ROLE_ENV_MAP[role_key]
    api_base = _clean_env(base_var, "")
    model_id = _clean_env(id_var, "")
    api_key = (_clean_env(key_var) or _clean_env(tok_var) or "EMPTY").strip()
    return api_base, model_id, api_key


def resolve_default_processor() -> str:
    """Resolves a local pre-cached tokenizer path if available, or falls back to 'gpt2'."""
    explicit = _clean_env("GUIDELLM_PROCESSOR", "")
    if explicit:
        return explicit
    default_cached = _clean_env("GUIDELLM_DEFAULT_PROCESSOR", "/opt/app-root/src/hf_cache/gpt2")
    if os.path.isdir(default_cached):
        return default_cached
    return "gpt2"


def _extract_stat_percentiles(stat_obj: Any, scale: float = 1.0) -> Tuple[float, float, float, float]:
    """Extracts (mean, p50, p95, p99) from various GuideLLM stat dictionary schemas."""
    if stat_obj is None:
        return 0.0, 0.0, 0.0, 0.0
    if isinstance(stat_obj, (int, float)):
        v = float(stat_obj) * scale
        return v, v, v, v
    if not isinstance(stat_obj, dict):
        return 0.0, 0.0, 0.0, 0.0

    # Unwrap nested "successful" or "total" blocks used in GuideLLM v0.2/v0.3
    for subkey in ("successful", "total", "summary", "distribution"):
        if isinstance(stat_obj.get(subkey), dict):
            inner = stat_obj[subkey]
            if any(k in inner for k in ("mean", "median", "percentiles", "p50", "p95")):
                stat_obj = inner
                break

    mean_val = float(stat_obj.get("mean") or stat_obj.get("average") or 0.0) * scale
    p50_val = float(stat_obj.get("median") or stat_obj.get("p50") or 0.0) * scale
    p95_val = float(stat_obj.get("p95") or 0.0) * scale
    p99_val = float(stat_obj.get("p99") or 0.0) * scale

    percentiles = stat_obj.get("percentiles")
    if isinstance(percentiles, dict):
        if p50_val == 0.0:
            p50_val = float(percentiles.get("p50") or percentiles.get("50") or percentiles.get("50.0") or 0.0) * scale
        if p95_val == 0.0:
            p95_val = float(percentiles.get("p95") or percentiles.get("95") or percentiles.get("95.0") or 0.0) * scale
        if p99_val == 0.0:
            p99_val = float(percentiles.get("p99") or percentiles.get("99") or percentiles.get("99.0") or 0.0) * scale

    if p50_val == 0.0 and mean_val > 0.0:
        p50_val = mean_val
    if p95_val == 0.0 and p50_val > 0.0:
        p95_val = p50_val
    if p99_val == 0.0 and p95_val > 0.0:
        p99_val = p95_val

    return round(mean_val, 2), round(p50_val, 2), round(p95_val, 2), round(p99_val, 2)


def _extract_scalar_stat(stat_obj: Any, default: float = 0.0) -> float:
    """Extracts a scalar mean/rate value from a GuideLLM stat field."""
    if stat_obj is None:
        return default
    if isinstance(stat_obj, (int, float)):
        return float(stat_obj)
    if isinstance(stat_obj, dict):
        for subkey in ("successful", "total"):
            if isinstance(stat_obj.get(subkey), dict):
                inner = stat_obj[subkey]
                val = inner.get("mean") or inner.get("rate") or inner.get("value") or inner.get("total")
                if val is not None:
                    return float(val)
        for k in ("mean", "rate", "value", "median", "p50", "total"):
            if stat_obj.get(k) is not None and isinstance(stat_obj.get(k), (int, float)):
                return float(stat_obj[k])
    return default


class BenchmarkTracker:
    """Tracks GuideLLM inference benchmark metrics, optimal concurrency, and SLO telemetry across pipeline stages."""

    _global_instance: Optional["BenchmarkTracker"] = None

    @classmethod
    def get_instance(cls, git_slug: Optional[str] = None, git_repo: Optional[str] = None) -> "BenchmarkTracker":
        if cls._global_instance is None:
            cls._global_instance = cls(git_slug=git_slug, git_repo=git_repo)
        else:
            if git_slug and not cls._global_instance.git_slug:
                cls._global_instance.git_slug = git_slug
            if git_repo and not cls._global_instance.git_repo:
                cls._global_instance.git_repo = git_repo
        return cls._global_instance

    @classmethod
    def reset_instance(cls, git_slug: Optional[str] = None, git_repo: Optional[str] = None) -> "BenchmarkTracker":
        cls._global_instance = cls(git_slug=git_slug, git_repo=git_repo)
        return cls._global_instance

    def __init__(
        self,
        git_slug: Optional[str] = None,
        git_repo: Optional[str] = None,
        run_id: Optional[str] = None,
    ):
        self.git_slug: Optional[str] = git_slug
        self.git_repo: Optional[str] = git_repo
        self.run_id: Optional[str] = run_id
        # List of normalized benchmark sweep points across endpoints and workloads
        self.records: List[Dict[str, Any]] = []
        # Map of role -> computed recommendation summary
        self.recommendations: Dict[str, Dict[str, Any]] = {}
        self.html_report_paths: List[str] = []
        self._merged_files: Set[str] = set()
        self._merged_runs: Set[str] = set()
        self._profile_sweeps_run: bool = False
        self._calibration_stage_cleared: bool = False

    def reset(self):
        self.records.clear()
        self.recommendations.clear()
        self.html_report_paths.clear()
        self._merged_files.clear()
        self._merged_runs.clear()
        self._profile_sweeps_run = False
        self._calibration_stage_cleared = False

    def add_record(
        self,
        role: str,
        workload: str,
        model_id: str,
        api_base: str,
        concurrency: int,
        requests_per_second: float,
        output_tokens_per_second: float,
        total_tokens_per_second: float,
        ttft_ms_mean: float,
        ttft_ms_p50: float,
        ttft_ms_p95: float,
        ttft_ms_p99: float,
        itl_ms_mean: float,
        itl_ms_p50: float,
        itl_ms_p95: float,
        itl_ms_p99: float,
        request_latency_sec_mean: float,
        request_latency_sec_p95: float,
        completed_requests: int = 0,
        errored_requests: int = 0,
        prompt_tokens: int = 0,
        output_tokens: int = 0,
        source: str = "guidellm",
    ):
        """Adds or updates a single benchmark rate point and recalculates optimal concurrency recommendations."""
        total_reqs = max(1, completed_requests + errored_requests)
        error_rate = round(errored_requests / total_reqs, 4) if errored_requests > 0 else 0.0

        rec = {
            "role": role,
            "workload": workload,
            "model_id": model_id,
            "api_base": api_base,
            "concurrency": int(concurrency),
            "requests_per_second": round(float(requests_per_second), 3),
            "output_tokens_per_second": round(float(output_tokens_per_second), 2),
            "total_tokens_per_second": round(float(total_tokens_per_second), 2),
            "ttft_ms_mean": round(float(ttft_ms_mean), 2),
            "ttft_ms_p50": round(float(ttft_ms_p50), 2),
            "ttft_ms_p95": round(float(ttft_ms_p95), 2),
            "ttft_ms_p99": round(float(ttft_ms_p99), 2),
            "itl_ms_mean": round(float(itl_ms_mean), 2),
            "itl_ms_p50": round(float(itl_ms_p50), 2),
            "itl_ms_p95": round(float(itl_ms_p95), 2),
            "itl_ms_p99": round(float(itl_ms_p99), 2),
            "request_latency_sec_mean": round(float(request_latency_sec_mean), 3),
            "request_latency_sec_p95": round(float(request_latency_sec_p95), 3),
            "completed_requests": int(completed_requests),
            "errored_requests": int(errored_requests),
            "error_rate": error_rate,
            "prompt_tokens": int(prompt_tokens),
            "output_tokens": int(output_tokens),
            "source": source,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        # Deduplicate by (role, workload, concurrency)
        replaced = False
        for idx, existing in enumerate(self.records):
            if (
                existing.get("role") == role
                and existing.get("workload") == workload
                and existing.get("concurrency") == int(concurrency)
            ):
                self.records[idx] = rec
                replaced = True
                break
        if not replaced:
            self.records.append(rec)

        self._recompute_recommendations()

    def _recompute_recommendations(self):
        """Computes optimal concurrency, RPM, and TPM per endpoint role based on recorded sweep points."""
        max_ttft_p95 = float(_clean_env("GUIDELLM_TTFT_P95_SLO_MS") or _clean_env("GUIDELLM_MAX_TTFT_P95_MS") or "15000")
        max_itl_p95 = float(_clean_env("GUIDELLM_ITL_P95_SLO_MS") or _clean_env("GUIDELLM_MAX_ITL_P95_MS") or "500")
        roles = sorted({r.get("role", "graphrag") for r in self.records})

        for role in roles:
            role_records = [r for r in self.records if r.get("role") == role]
            if not role_records:
                continue

            # Filter candidates with zero (or lowest) error rate and acceptable TTFT/ITL p95
            healthy = [
                r for r in role_records
                if r.get("error_rate", 0.0) == 0.0
                and r.get("output_tokens_per_second", 0.0) > 0.0
                and (r.get("ttft_ms_p95", 0.0) <= max_ttft_p95 or r.get("ttft_ms_p95", 0.0) == 0.0)
                and (r.get("itl_ms_p95", 0.0) <= max_itl_p95 or r.get("itl_ms_p95", 0.0) == 0.0)
            ]
            if not healthy:
                healthy = [
                    r for r in role_records
                    if r.get("error_rate", 0.0) == 0.0 and r.get("output_tokens_per_second", 0.0) > 0.0
                ]
            if not healthy:
                healthy = [r for r in role_records if r.get("output_tokens_per_second", 0.0) > 0.0] or role_records

            peak_tok_s = max((r.get("output_tokens_per_second", 0.0) for r in healthy), default=0.0)
            # Pick the smallest concurrency that achieves >= 88% of peak output throughput (knee of the curve)
            knee_candidates = [
                r for r in sorted(healthy, key=lambda x: x.get("concurrency", 1))
                if r.get("output_tokens_per_second", 0.0) >= 0.88 * peak_tok_s
            ]
            best = knee_candidates[0] if knee_candidates else max(healthy, key=lambda x: x.get("output_tokens_per_second", 0.0))

            opt_conc = max(1, int(best.get("concurrency", 10)))
            rps = float(best.get("requests_per_second", 0.0))
            out_tok_s = float(best.get("output_tokens_per_second", 0.0))
            tot_tok_s = float(best.get("total_tokens_per_second", 0.0)) or (out_tok_s * 2.5)

            # Set safe RPM and TPM ceilings at 90% of sustained capacity (with sensible minimums)
            rec_rpm = max(10, int(rps * 60 * 0.9)) if rps > 0 else 60
            rec_tpm = max(4000, int(tot_tok_s * 60 * 0.9)) if tot_tok_s > 0 else 60000
            parallel_repos = max(1, int(_clean_env("GRAPHRAG_PARALLEL_REPOS", "2")))
            sdg_conc = max(2, opt_conc // parallel_repos) if opt_conc >= 4 else opt_conc

            self.recommendations[role] = {
                "role": role,
                "model_id": best.get("model_id", ""),
                "api_base": best.get("api_base", ""),
                "workload": best.get("workload", "indexing"),
                "optimal_concurrency": opt_conc,
                "recommended_sdg_concurrency": sdg_conc,
                "recommended_graphrag_concurrency": opt_conc,
                "recommended_rpm": rec_rpm,
                "recommended_tpm": rec_tpm,
                "sustained_output_tokens_per_second": round(out_tok_s, 2),
                "sustained_total_tokens_per_second": round(tot_tok_s, 2),
                "sustained_requests_per_second": round(rps, 3),
                "ttft_ms_p50": best.get("ttft_ms_p50", 0.0),
                "ttft_ms_p95": best.get("ttft_ms_p95", 0.0),
                "ttft_ms_p99": best.get("ttft_ms_p99", 0.0),
                "itl_ms_p50": best.get("itl_ms_p50", 0.0),
                "itl_ms_p95": best.get("itl_ms_p95", 0.0),
                "itl_ms_p99": best.get("itl_ms_p99", 0.0),
                "error_rate": best.get("error_rate", 0.0),
            }

    @property
    def profiles(self) -> List[Dict[str, Any]]:
        """Alias for self.records so pipeline stages can inspect calibrated GuideLLM profiles."""
        return self.records

    def get_recommended_sdg_concurrency(self, default: int = 10, role: str = "graphrag") -> int:
        """Returns recommended SDG_LLM_MAX_CONCURRENCY from GuideLLM calibration or default."""
        if (
            not self._profile_sweeps_run
            and not self._calibration_stage_cleared
            and self.git_slug
            and _clean_env("GUIDELLM_CALIBRATE_PIPELINE", "false").lower() not in ("true", "1", "yes")
        ):
            try:
                from utils.duration_tracker import DurationTracker
                dur_tracker = DurationTracker.get_instance(git_slug=self.git_slug, git_repo=self.git_repo)
                if not dur_tracker.records:
                    dur_tracker.clear_mlflow_stage(git_slug=self.git_slug, stage="Calibration")
                    self._calibration_stage_cleared = True
            except Exception as e:
                logging.debug("Failed clearing Calibration stage in get_recommended_sdg_concurrency: %s", e)

        rec = self.recommendations.get(role) or self.recommendations.get("graphrag")
        if not rec:
            return default
        return int(rec.get("recommended_sdg_concurrency") or rec.get("optimal_concurrency") or default)

    def get_recommended_graphrag_limits(
        self, role: str = "graphrag", default_concurrency: int = 25
    ) -> Dict[str, Any]:
        """Returns recommended (concurrent_requests, requests_per_minute, tokens_per_minute, sdg_concurrency)."""
        rec = self.recommendations.get(role) or self.recommendations.get("graphrag")
        if not rec:
            return {
                "concurrent_requests": default_concurrency,
                "requests_per_minute": "auto",
                "tokens_per_minute": "auto",
                "sdg_max_concurrency": default_concurrency,
                "sustained_output_tokens_per_second": 0.0,
            }
        return {
            "concurrent_requests": rec.get("recommended_graphrag_concurrency", default_concurrency),
            "requests_per_minute": rec.get("recommended_rpm", "auto"),
            "tokens_per_minute": rec.get("recommended_tpm", "auto"),
            "sdg_max_concurrency": rec.get("recommended_sdg_concurrency", default_concurrency),
            "sustained_output_tokens_per_second": rec.get("sustained_output_tokens_per_second", 0.0),
        }

    def run_guidellm_profile(
        self,
        profile_name: str = "sdg",
        rates: Optional[List[int]] = None,
        max_seconds: int = 15,
        role: str = "graphrag",
        output_dir: Optional[str] = None,
    ) -> int:
        """Convenience wrapper around run_benchmark for pipeline stage calibration."""
        self._profile_sweeps_run = True
        rates_str = ",".join(str(r) for r in rates) if rates else None

        dur_tracker = None
        outer_active = False
        try:
            from utils.duration_tracker import DurationTracker
            dur_tracker = DurationTracker.get_instance(git_slug=self.git_slug, git_repo=self.git_repo)
            dur_tracker.record_pod_creation_overhead(stage="Calibration")
            outer_active = any(
                a.get("stage") == "Calibration" and a.get("step") == "guidellm-calibration-op"
                for a in getattr(dur_tracker, "_active_measurements", [])
            )
        except Exception as e:
            logging.debug("Failed to initialize Calibration DurationTracker in run_guidellm_profile: %s", e)

        t0 = time.perf_counter()
        start_wall = time.time()
        status = "success"
        try:
            return self.run_benchmark(
                role=role,
                workload=profile_name,
                rates_str=rates_str,
                max_seconds=max_seconds,
                output_dir=output_dir,
            )
        except Exception:
            status = "failed"
            raise
        finally:
            if dur_tracker is not None and not outer_active:
                try:
                    elapsed = max(0.0, time.perf_counter() - t0)
                    end_wall = time.time()
                    existing = next(
                        (
                            r for r in dur_tracker.records
                            if r.get("stage") == "Calibration" and r.get("step") == "guidellm-calibration-op"
                        ),
                        None,
                    )
                    prev_dur = float(existing.get("duration", 0.0)) if existing else 0.0
                    first_start = float(existing.get("start_time", start_wall)) if existing else start_wall
                    dur_tracker.record_step(
                        stage="Calibration",
                        step="guidellm-calibration-op",
                        duration=prev_dur + elapsed,
                        start_time=first_start,
                        end_time=end_wall,
                        status=status,
                        metadata={"profile": profile_name, "rates": rates or []},
                    )
                except Exception as e:
                    logging.debug("Failed recording Calibration step duration: %s", e)

    def estimate_pipeline_runtime(self, total_files: int, total_lines: int = 0) -> Optional[Dict[str, Any]]:
        """Estimates Stage 1 (SDG) + Stage 2 (Indexing) + Stage 3 (Analysis) runtime using GuideLLM tok/s."""
        rec = self.recommendations.get("graphrag")
        if not rec or total_files <= 0:
            return None
        out_tok_s = float(rec.get("sustained_output_tokens_per_second", 0.0))
        if out_tok_s <= 0:
            return None

        # Empirical output tokens generated per repository file across AUGUR's 3 stages:
        # Stage 1 (SDG JSON metadata): ~400 output tokens/file
        # Stage 2 (GraphRAG extract_graph + summarize + community_reports): ~750 output tokens/file
        # Stage 3 (12 migration prompts): ~18,000 output tokens fixed
        sdg_output_tokens = total_files * 400
        indexing_output_tokens = total_files * 750
        analysis_output_tokens = 18000

        sdg_sec = sdg_output_tokens / out_tok_s
        indexing_sec = indexing_output_tokens / out_tok_s
        analysis_sec = analysis_output_tokens / out_tok_s
        total_sec = (sdg_sec + indexing_sec + analysis_sec) * 1.15  # 15% non-LLM I/O & embedding overhead

        return {
            "total_files": total_files,
            "total_lines": total_lines,
            "sustained_output_tokens_per_second": out_tok_s,
            "optimal_concurrency": rec.get("optimal_concurrency", 10),
            "estimated_sdg_seconds": round(sdg_sec, 1),
            "estimated_indexing_seconds": round(indexing_sec, 1),
            "estimated_analysis_seconds": round(analysis_sec, 1),
            "estimated_total_seconds": round(total_sec, 1),
            "estimated_total_minutes": round(total_sec / 60.0, 1),
            "stage1_sdg_seconds": round(sdg_sec, 1),
            "stage2_indexing_seconds": round(indexing_sec, 1),
            "stage3_analysis_seconds": round(analysis_sec, 1),
            "total_seconds": round(total_sec, 1),
            "total_minutes": round(total_sec / 60.0, 1),
        }

    def parse_guidellm_json(
        self,
        filepath: str,
        role: str = "graphrag",
        workload: str = "indexing",
        model_id: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> int:
        """Parses a GuideLLM output JSON file (v0.2+ or v0.3+ schema) and adds records to this tracker."""
        if not filepath or not os.path.isfile(filepath):
            return 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logging.warning("BenchmarkTracker: Failed to parse GuideLLM JSON '%s': %s", filepath, e)
            return 0

        # If this is already a serialized BenchmarkTracker file, load_and_merge it
        if isinstance(data, dict) and "records" in data and "recommendations" in data:
            self.load_and_merge(filepath)
            return len(data.get("records", []))

        resolved_base, resolved_model, _ = resolve_role_credentials(role)
        model_id = model_id or resolved_model or "unknown"
        api_base = api_base or resolved_base or "unknown"
        profile_cfg = AUGUR_WORKLOAD_PROFILES.get(workload, AUGUR_WORKLOAD_PROFILES["indexing"])

        benchmarks = []
        if isinstance(data, dict):
            benchmarks = data.get("benchmarks") or data.get("runs") or data.get("results") or []
            if not benchmarks and ("metrics" in data or "stats" in data):
                benchmarks = [data]
        elif isinstance(data, list):
            benchmarks = data

        added = 0
        for idx, b in enumerate(benchmarks):
            if not isinstance(b, dict):
                continue

            metrics = b.get("metrics") or b.get("stats") or b
            scheduler_info = b.get("scheduler") or b.get("strategy") or b.get("rate") or b.get("args") or {}

            # Determine concurrency / rate level
            concurrency = 0
            for cand_obj in (b, scheduler_info, metrics):
                if isinstance(cand_obj, dict):
                    for k in ("concurrency", "max_concurrency", "streams", "rate", "num_workers"):
                        val = cand_obj.get(k)
                        if isinstance(val, (int, float)) and val > 0:
                            concurrency = int(round(val))
                            break
                if concurrency > 0:
                    break
            if concurrency <= 0:
                concurrency = idx + 1

            # Extract TTFT (convert seconds -> ms if needed)
            ttft_raw = (
                metrics.get("time_to_first_token_ms")
                or metrics.get("ttft_ms")
                or metrics.get("time_to_first_token")
                or metrics.get("ttft")
            )
            ttft_scale = 1000.0 if ("time_to_first_token" in metrics and "time_to_first_token_ms" not in metrics) else 1.0
            ttft_mean, ttft_p50, ttft_p95, ttft_p99 = _extract_stat_percentiles(ttft_raw, scale=ttft_scale)
            # Heuristic: if TTFT is < 2.0 and likely in seconds, scale to ms
            if 0.0 < ttft_p50 < 1.5 and ttft_scale == 1.0 and "ms" not in str(metrics.keys()):
                ttft_mean, ttft_p50, ttft_p95, ttft_p99 = [round(x * 1000.0, 2) for x in (ttft_mean, ttft_p50, ttft_p95, ttft_p99)]

            # Extract ITL (Inter-Token Latency)
            itl_raw = (
                metrics.get("inter_token_latency_ms")
                or metrics.get("itl_ms")
                or metrics.get("time_per_output_token_ms")
                or metrics.get("inter_token_latency")
                or metrics.get("itl")
            )
            itl_scale = 1000.0 if ("inter_token_latency" in metrics and "inter_token_latency_ms" not in metrics) else 1.0
            itl_mean, itl_p50, itl_p95, itl_p99 = _extract_stat_percentiles(itl_raw, scale=itl_scale)
            if 0.0 < itl_p50 < 0.5 and itl_scale == 1.0:
                itl_mean, itl_p50, itl_p95, itl_p99 = [round(x * 1000.0, 2) for x in (itl_mean, itl_p50, itl_p95, itl_p99)]

            # Extract request latency (seconds)
            lat_raw = metrics.get("request_latency") or metrics.get("request_latency_seconds") or metrics.get("latency")
            lat_mean, _, lat_p95, _ = _extract_stat_percentiles(lat_raw, scale=1.0)

            # Extract throughput
            rps = _extract_scalar_stat(metrics.get("requests_per_second") or metrics.get("request_throughput"))
            out_tok_s = _extract_scalar_stat(
                metrics.get("output_tokens_per_second") or metrics.get("output_token_throughput")
            )
            tot_tok_s = _extract_scalar_stat(
                metrics.get("tokens_per_second") or metrics.get("total_tokens_per_second")
            )
            if tot_tok_s <= 0.0 and out_tok_s > 0.0:
                tot_tok_s = out_tok_s * (
                    1.0 + (profile_cfg["prompt_tokens"] / max(1, profile_cfg["output_tokens"]))
                )

            # Request counts
            req_totals = b.get("request_totals") or metrics.get("request_totals") or {}
            completed = int(
                req_totals.get("successful")
                or req_totals.get("completed")
                or _extract_scalar_stat(metrics.get("completed_requests"), default=1.0)
            )
            errored = int(
                req_totals.get("errored")
                or req_totals.get("failed")
                or _extract_scalar_stat(metrics.get("errored_requests"), default=0.0)
            )

            self.add_record(
                role=role,
                workload=workload,
                model_id=model_id,
                api_base=api_base,
                concurrency=concurrency,
                requests_per_second=rps,
                output_tokens_per_second=out_tok_s,
                total_tokens_per_second=tot_tok_s,
                ttft_ms_mean=ttft_mean,
                ttft_ms_p50=ttft_p50,
                ttft_ms_p95=ttft_p95,
                ttft_ms_p99=ttft_p99,
                itl_ms_mean=itl_mean,
                itl_ms_p50=itl_p50,
                itl_ms_p95=itl_p95,
                itl_ms_p99=itl_p99,
                request_latency_sec_mean=lat_mean,
                request_latency_sec_p95=lat_p95,
                completed_requests=completed,
                errored_requests=errored,
                prompt_tokens=profile_cfg["prompt_tokens"],
                output_tokens=profile_cfg["output_tokens"],
                source="guidellm",
            )
            added += 1

        return added

    async def _run_builtin_async_probe(
        self,
        role: str,
        workload: str,
        api_base: str,
        model_id: str,
        api_key: str,
        rates: List[int],
        max_seconds: int,
        prompt_tokens: int,
        output_tokens: int,
    ) -> int:
        """Benchmark LLM endpoint concurrency via streaming SSE requests.

        Uses httpx if installed, falling back to urllib.request and a thread pool.
        """
        import concurrent.futures
        import ssl
        import urllib.error
        import urllib.request

        try:
            import httpx  # type: ignore
            has_httpx = True
        except ImportError:
            httpx = None  # type: ignore
            has_httpx = False

        verify_tls = _clean_env("ENABLE_TLS_VERIFICATION", "false").lower() in ("true", "1", "yes")
        ca_bundle = _clean_env("SSL_CERT_FILE") or _clean_env("REQUESTS_CA_BUNDLE")

        ssl_ctx = ssl.create_default_context()
        if not verify_tls:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        elif ca_bundle and os.path.exists(ca_bundle):
            ssl_ctx.load_verify_locations(cafile=ca_bundle)

        base_clean = api_base.rstrip("/")
        if base_clean.endswith("/chat/completions"):
            chat_url = base_clean
        elif base_clean.endswith("/v1") or "/v1/" in base_clean:
            chat_url = f"{base_clean}/chat/completions"
        else:
            chat_url = f"{base_clean}/v1/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_key or 'EMPTY'}",
        }

        http_client = None
        if has_httpx:
            verify_arg: Union[bool, str] = (
                ca_bundle if (verify_tls and ca_bundle and os.path.exists(ca_bundle)) else verify_tls
            )
            http_client = httpx.AsyncClient(verify=verify_arg, timeout=httpx.Timeout(120.0, connect=15.0))

        # Synthetic prompt roughly matching requested prompt_tokens (~4 chars/token)
        base_snippet = (
            "Analyze the following software module dependencies, exported symbols, "
            "and configuration keys for enterprise OpenShift modernization:\n"
        )
        repeat_unit = "def process_service_request(ctx, config, db_conn): return db_conn.execute(ctx.query)\n"
        target_chars = max(120, prompt_tokens * 4)
        synthetic_prompt = base_snippet + (repeat_unit * max(1, (target_chars // len(repeat_unit))))[:target_chars]

        max_conc = max([1] + [int(r) for r in rates])
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(16, max_conc + 4))
        logged_cluster_hint = False

        def _parse_sse_line(line: str) -> Optional[Dict[str, Any]]:
            line = line.strip()
            if not line or line.startswith(":"):
                return None
            data_str = line[5:].strip() if line.startswith("data:") else line
            if data_str == "[DONE]":
                return {"__done__": True}
            try:
                parsed = json.loads(data_str)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None

        class _StreamMetrics:
            def __init__(self, start_time: float):
                self.start_time = start_time
                self.first_token_t: Optional[float] = None
                self.prev_token_t: Optional[float] = None
                self.chunk_gaps: List[float] = []
                self.chunks_count = 0
                self.char_count = 0
                self.usage_out_tokens = 0
                self.usage_in_tokens = 0

            def feed_chunk(self, chunk: Dict[str, Any], now: float) -> bool:
                if chunk.get("__done__"):
                    return False
                usage = chunk.get("usage")
                if isinstance(usage, dict):
                    self.usage_out_tokens = max(self.usage_out_tokens, int(usage.get("completion_tokens") or 0))
                    self.usage_in_tokens = max(self.usage_in_tokens, int(usage.get("prompt_tokens") or 0))

                choices = chunk.get("choices")
                if isinstance(choices, list) and choices:
                    c0 = choices[0] if isinstance(choices[0], dict) else {}
                    delta = c0.get("delta") or c0.get("message") or {}
                    delta_text = ""
                    if isinstance(delta, dict):
                        delta_text = str(
                            delta.get("content")
                            or delta.get("reasoning_content")
                            or delta.get("reasoning")
                            or delta.get("text")
                            or ""
                        )
                    if delta_text:
                        self.chunks_count += 1
                        self.char_count += len(delta_text)
                        if self.first_token_t is None:
                            self.first_token_t = now
                        elif self.prev_token_t is not None:
                            self.chunk_gaps.append((now - self.prev_token_t) * 1000.0)
                        self.prev_token_t = now
                    elif self.first_token_t is None:
                        self.first_token_t = now
                        self.prev_token_t = now
                return True

            def calculate(self) -> Tuple[float, float, Optional[float], int, int]:
                req_dur = max(0.001, time.perf_counter() - self.start_time)
                if self.first_token_t is not None:
                    ttft = (self.first_token_t - self.start_time) * 1000.0
                    if self.chunks_count <= 1 and ttft >= req_dur * 900.0:
                        ttft = req_dur * 250.0
                    ttft = max(1.0, ttft)
                else:
                    ttft = req_dur * 250.0

                est_out = (
                    self.usage_out_tokens
                    or max(self.chunks_count, int(self.char_count / 3.8))
                    or max(16, output_tokens // 2)
                )
                if self.chunk_gaps:
                    itl = sum(self.chunk_gaps) / len(self.chunk_gaps)
                elif est_out > 1:
                    itl = max(1.0, (req_dur * 1000.0) - ttft) / max(1, est_out - 1)
                else:
                    itl = None

                est_in = self.usage_in_tokens or prompt_tokens
                return req_dur, ttft, itl, est_out, est_in

        def _sync_urllib_stream(payload: dict) -> Tuple[float, float, Optional[float], int, int]:
            metrics = _StreamMetrics(time.perf_counter())
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(chat_url, data=data, headers=headers, method="POST")
            try:
                resp = urllib.request.urlopen(req, context=ssl_ctx, timeout=120.0)
            except urllib.error.HTTPError as err:
                if "stream_options" in payload and err.code == 400:
                    retry_payload = dict(payload)
                    retry_payload.pop("stream_options", None)
                    return _sync_urllib_stream(retry_payload)
                raise

            with resp:
                for raw_line in resp:
                    chunk = _parse_sse_line(raw_line.decode("utf-8", errors="replace"))
                    if chunk and not metrics.feed_chunk(chunk, time.perf_counter()):
                        break
            return metrics.calculate()

        async def _async_httpx_stream(payload: dict) -> Tuple[float, float, Optional[float], int, int]:
            metrics = _StreamMetrics(time.perf_counter())
            async with http_client.stream("POST", chat_url, headers=headers, json=payload) as resp:
                if "stream_options" in payload and resp.status_code == 400:
                    retry_payload = dict(payload)
                    retry_payload.pop("stream_options", None)
                    return await _async_httpx_stream(retry_payload)
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    chunk = _parse_sse_line(line)
                    if chunk and not metrics.feed_chunk(chunk, time.perf_counter()):
                        break
            return metrics.calculate()

        added = 0
        try:
            for conc in rates:
                conc = max(1, int(conc))
                per_rate_budget = max(8.0, float(max_seconds) / max(1, len(rates)))
                start_wall = time.perf_counter()

                ttfts_ms: List[float] = []
                itls_ms: List[float] = []
                latencies_s: List[float] = []
                total_out_tokens = 0
                total_in_tokens = 0
                completed = 0
                errored = 0
                logged_error_for_rate = False

                async def _single_stream_call() -> None:
                    nonlocal total_out_tokens, total_in_tokens, completed, errored, logged_error_for_rate, logged_cluster_hint
                    try:
                        payload = {
                            "model": model_id,
                            "messages": [{"role": "user", "content": synthetic_prompt}],
                            "max_tokens": output_tokens,
                            "temperature": 0.1,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        }
                        if http_client is not None:
                            req_dur, ttft, itl, est_out, est_in = await _async_httpx_stream(payload)
                        else:
                            loop = asyncio.get_running_loop()
                            req_dur, ttft, itl, est_out, est_in = await loop.run_in_executor(
                                executor, _sync_urllib_stream, payload
                            )

                        latencies_s.append(req_dur)
                        ttfts_ms.append(ttft)
                        if itl is not None:
                            itls_ms.append(itl)
                        total_out_tokens += est_out
                        total_in_tokens += est_in
                        completed += 1
                    except Exception as exc:
                        if not logged_error_for_rate:
                            logged_error_for_rate = True
                            logging.warning("Benchmark request error at concurrency=%d (%s): %s", conc, chat_url, exc)
                            if ".svc.cluster.local" in chat_url and not logged_cluster_hint:
                                logged_cluster_hint = True
                                logging.warning(
                                    "Hint: '%s' is an internal Kubernetes service URL. To benchmark from outside the cluster, "
                                    "run 'make benchmark-llm' (runs inside OpenShift) or use an external Route URL.",
                                    api_base,
                                )
                        errored += 1

                while (time.perf_counter() - start_wall) < per_rate_budget:
                    await asyncio.gather(*[_single_stream_call() for _ in range(conc)])
                    if errored >= conc and completed == 0:
                        break

                elapsed = max(0.001, time.perf_counter() - start_wall)

                def _pct(vals: List[float], q: float) -> float:
                    if not vals:
                        return 0.0
                    s = sorted(vals)
                    idx = min(len(s) - 1, max(0, int(round((q / 100.0) * (len(s) - 1)))))
                    return round(s[idx], 2)

                def _mean(vals: List[float]) -> float:
                    return round(sum(vals) / len(vals), 2) if vals else 0.0

                self.add_record(
                    role=role,
                    workload=workload,
                    model_id=model_id,
                    api_base=api_base,
                    concurrency=conc,
                    requests_per_second=completed / elapsed,
                    output_tokens_per_second=total_out_tokens / elapsed,
                    total_tokens_per_second=(total_in_tokens + total_out_tokens) / elapsed,
                    ttft_ms_mean=_mean(ttfts_ms),
                    ttft_ms_p50=_pct(ttfts_ms, 50),
                    ttft_ms_p95=_pct(ttfts_ms, 95),
                    ttft_ms_p99=_pct(ttfts_ms, 99),
                    itl_ms_mean=_mean(itls_ms),
                    itl_ms_p50=_pct(itls_ms, 50),
                    itl_ms_p95=_pct(itls_ms, 95),
                    itl_ms_p99=_pct(itls_ms, 99),
                    request_latency_sec_mean=_mean(latencies_s),
                    request_latency_sec_p95=_pct(latencies_s, 95),
                    completed_requests=completed,
                    errored_requests=errored,
                    prompt_tokens=prompt_tokens,
                    output_tokens=output_tokens,
                    source="guidellm-async-probe",
                )
                added += 1
        finally:
            if http_client is not None:
                await http_client.aclose()
            executor.shutdown(wait=False)

        return added

    def run_benchmark(
        self,
        role: str = "graphrag",
        workload: str = "indexing",
        profile: Optional[str] = None,
        rates_str: Optional[str] = None,
        max_seconds: Optional[int] = None,
        output_dir: Optional[str] = None,
    ) -> int:
        """Executes a GuideLLM benchmark against the specified role and records results."""
        api_base, model_id, api_key = resolve_role_credentials(role)
        if not api_base or not model_id:
            logging.warning(
                "BenchmarkTracker: Skipping benchmark for role='%s' (API_BASE='%s', MODEL_ID='%s').",
                role, api_base, model_id,
            )
            return 0

        w_cfg = AUGUR_WORKLOAD_PROFILES.get(workload, AUGUR_WORKLOAD_PROFILES["indexing"])
        raw_profile = (profile or _clean_env("GUIDELLM_PROFILE") or w_cfg["default_profile"]).lower()
        # If GUIDELLM_PROFILE was set to a workload name (e.g. 'all', 'sdg', 'indexing'), use default_profile ('concurrent')
        if raw_profile in ("precheck", "sdg", "indexing", "analysis", "all"):
            profile = w_cfg["default_profile"]
        else:
            profile = raw_profile

        rates_str = (
            rates_str
            or _clean_env("GUIDELLM_CONCURRENCY_RATES")
            or _clean_env("GUIDELLM_RATES")
            or w_cfg["default_rates"]
        )
        max_seconds = int(max_seconds or _clean_env("GUIDELLM_MAX_SECONDS") or w_cfg["default_max_seconds"])
        prompt_tokens = int(_clean_env("GUIDELLM_PROMPT_TOKENS") or w_cfg["prompt_tokens"])
        output_tokens = int(_clean_env("GUIDELLM_OUTPUT_TOKENS") or w_cfg["output_tokens"])
        processor = resolve_default_processor()

        rates_list = [int(x.strip()) for x in rates_str.split(",") if x.strip().isdigit()] or [5]
        work_dir = output_dir or tempfile.mkdtemp(prefix=f"guidellm_{role}_{workload}_")
        os.makedirs(work_dir, exist_ok=True)
        json_out = os.path.join(work_dir, f"guidellm_{role}_{workload}.json")

        guidellm_bin = shutil.which("guidellm")
        use_cli = _clean_env("GUIDELLM_USE_CLI", "true").lower() in ("true", "1", "yes")
        if guidellm_bin and use_cli:
            env = dict(os.environ)
            env["OPENAI_API_KEY"] = api_key
            env["GUIDELLM__OPENAI__API_KEY"] = api_key
            if _clean_env("ENABLE_TLS_VERIFICATION", "false").lower() not in ("true", "1", "yes"):
                env["GUIDELLM__OPENAI__VERIFY"] = "false"
                env["PYTHONHTTPSVERIFY"] = "0"

            # Detect GuideLLM CLI subcommand across v0.2 (openai), v0.3-v0.6 (benchmark [run]), and v0.7+ (run)
            cmd: Optional[List[str]] = None
            try:
                help_proc = subprocess.run([guidellm_bin, "--help"], env=env, capture_output=True, text=True, timeout=10)
                help_txt = (help_proc.stdout or "") + (help_proc.stderr or "")
                rates_csv = ",".join(str(r) for r in rates_list)

                if re.search(r"\bbenchmark\b", help_txt):
                    sub_proc = subprocess.run([guidellm_bin, "benchmark", "--help"], env=env, capture_output=True, text=True, timeout=10)
                    sub_txt = (sub_proc.stdout or "") + (sub_proc.stderr or "")
                    sub_cmd = [guidellm_bin, "benchmark", "run"] if re.search(r"\brun\b", sub_txt) else [guidellm_bin, "benchmark"]
                    cmd = sub_cmd + [
                        "--target", api_base,
                        "--model", model_id,
                        "--processor", processor,
                        "--data", f"prompt_tokens={prompt_tokens},output_tokens={output_tokens}",
                        "--max-seconds", str(max_seconds),
                        "--output-path", json_out,
                    ]
                    if profile in ("concurrent", "throughput", "synchronous"):
                        cmd.extend(["--rate-type", profile])
                        if profile == "concurrent":
                            cmd.extend(["--rate", rates_csv])
                    else:
                        cmd.extend(["--profile", profile])
                elif re.search(r"\brun\b", help_txt):
                    sub_proc = subprocess.run([guidellm_bin, "run", "--help"], env=env, capture_output=True, text=True, timeout=10)
                    sub_txt = (sub_proc.stdout or "") + (sub_proc.stderr or "")
                    target_flag = "--target" if "--target" in sub_txt else "--backend"
                    out_flag = "--output-path" if "--output-path" in sub_txt else "--output-dir"
                    out_val = json_out if out_flag == "--output-path" else work_dir
                    cmd = [
                        guidellm_bin,
                        "run",
                        target_flag, api_base,
                        "--model", model_id,
                        "--processor", processor,
                        "--data", f"prompt_tokens={prompt_tokens},output_tokens={output_tokens}",
                        "--max-seconds", str(max_seconds),
                        out_flag, out_val,
                    ]
                    if "--rate-type" in sub_txt:
                        cmd.extend(["--rate-type", profile])
                        if profile == "concurrent":
                            cmd.extend(["--rate", rates_csv])
                    else:
                        cmd.extend(["--profile", profile])
                        if profile == "concurrent" and "--rate" in sub_txt:
                            cmd.extend(["--rate", rates_csv])
                        elif profile == "concurrent" and "--rates" in sub_txt:
                            cmd.extend(["--rates", rates_csv])
            except Exception as det_err:
                logging.debug("GuideLLM CLI subcommand detection skipped: %s", det_err)

            if cmd:
                logging.info(
                    "Running GuideLLM CLI benchmark for role='%s', workload='%s', model='%s' (rates=%s, max_seconds=%ds)...",
                    role, workload, model_id, rates_list, max_seconds,
                )
                try:
                    proc = subprocess.run(
                        cmd, env=env, capture_output=True, text=True, timeout=max_seconds * (len(rates_list) + 2) + 60
                    )
                    if not os.path.isfile(json_out):
                        for cand_name in ("benchmarks.json", "results.json"):
                            cand_p = os.path.join(work_dir, cand_name)
                            if os.path.isfile(cand_p):
                                json_out = cand_p
                                break
                    if proc.returncode == 0 and os.path.isfile(json_out):
                        html_candidate = os.path.join(work_dir, "benchmarks.html")
                        if os.path.isfile(html_candidate) and html_candidate not in self.html_report_paths:
                            self.html_report_paths.append(html_candidate)
                        count = self.parse_guidellm_json(
                            json_out, role=role, workload=workload, model_id=model_id, api_base=api_base
                        )
                        rec = self.recommendations.get(role) or {}
                        if count > 0 and float(rec.get("sustained_output_tokens_per_second", 0.0)) > 1.0:
                            return count
                        logging.info(
                            "GuideLLM CLI reported low/unparsed token throughput (%.2f tok/s, likely a reasoning model); "
                            "running built-in streaming probe for accurate token & TTFT telemetry...",
                            float(rec.get("sustained_output_tokens_per_second", 0.0)),
                        )
                    else:
                        logging.info(
                            "GuideLLM CLI exited with code %s; using built-in OpenAI streaming probe.",
                            proc.returncode,
                        )
                except Exception as e:
                    logging.info("GuideLLM CLI execution skipped (%s); using built-in OpenAI streaming probe.", e)

        logging.info(
            "Running built-in OpenAI streaming benchmark probe for role='%s', workload='%s', model='%s' (rates=%s, max_seconds=%ds)...",
            role, workload, model_id, rates_list, max_seconds,
        )
        return asyncio.run(
            self._run_builtin_async_probe(
                role=role,
                workload=workload,
                api_base=api_base,
                model_id=model_id,
                api_key=api_key,
                rates=rates_list,
                max_seconds=max_seconds,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "records": self.records,
            "recommendations": self.recommendations,
        }
        for attr in ("git_slug", "git_repo", "run_id"):
            val = getattr(self, attr)
            if val:
                d[attr] = val
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BenchmarkTracker":
        tracker = cls(
            git_slug=data.get("git_slug"),
            git_repo=data.get("git_repo"),
            run_id=data.get("run_id"),
        )
        tracker.records = list(data.get("records", []))
        tracker.recommendations = dict(data.get("recommendations", {}))
        if tracker.records and not tracker.recommendations:
            tracker._recompute_recommendations()
        return tracker

    def save_to_file(self, filepath: str):
        if not filepath:
            return
        if not self.records and os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    if json.load(f).get("records"):
                        return
            except Exception:
                pass
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load_from_file(cls, filepath: str) -> Optional["BenchmarkTracker"]:
        if not filepath or not os.path.isfile(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and ("records" in data or "recommendations" in data):
                return cls.from_dict(data)
            tracker = cls()
            tracker.parse_guidellm_json(filepath)
            return tracker if tracker.records else None
        except Exception as e:
            logging.debug("Failed loading benchmarks from %s: %s", filepath, e)
            return None

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        if not filepath or not os.path.isfile(filepath):
            return
        norm_path = os.path.normpath(os.path.abspath(filepath))
        if norm_path in self._merged_files:
            return

        other = self.load_from_file(filepath)
        if not other or not other.records:
            return

        self.git_slug = self.git_slug or other.git_slug
        self.git_repo = self.git_repo or other.git_repo
        self.run_id = self.run_id or other.run_id

        for r in other.records:
            self.add_record(
                role=r.get("role", "graphrag"),
                workload=r.get("workload", "indexing"),
                model_id=r.get("model_id", ""),
                api_base=r.get("api_base", ""),
                concurrency=r.get("concurrency", 1),
                requests_per_second=r.get("requests_per_second", 0.0),
                output_tokens_per_second=r.get("output_tokens_per_second", 0.0),
                total_tokens_per_second=r.get("total_tokens_per_second", 0.0),
                ttft_ms_mean=r.get("ttft_ms_mean", 0.0),
                ttft_ms_p50=r.get("ttft_ms_p50", 0.0),
                ttft_ms_p95=r.get("ttft_ms_p95", 0.0),
                ttft_ms_p99=r.get("ttft_ms_p99", 0.0),
                itl_ms_mean=r.get("itl_ms_mean", 0.0),
                itl_ms_p50=r.get("itl_ms_p50", 0.0),
                itl_ms_p95=r.get("itl_ms_p95", 0.0),
                itl_ms_p99=r.get("itl_ms_p99", 0.0),
                request_latency_sec_mean=r.get("request_latency_sec_mean", 0.0),
                request_latency_sec_p95=r.get("request_latency_sec_p95", 0.0),
                completed_requests=r.get("completed_requests", 0),
                errored_requests=r.get("errored_requests", 0),
                prompt_tokens=r.get("prompt_tokens", 0),
                output_tokens=r.get("output_tokens", 0),
                source=r.get("source", "guidellm"),
            )

        self._merged_files.add(norm_path)

    def log_to_mlflow(self, run_id: Optional[str] = None):
        """Logs GuideLLM latency, throughput, and optimal concurrency metrics to MLflow."""
        if not self.recommendations:
            return
        try:
            import mlflow
            metrics: Dict[str, float] = {}
            for role, rec in self.recommendations.items():
                prefix = f"guidellm_{re.sub(r'[^a-zA-Z0-9_]', '_', role.lower())}"
                metrics[f"{prefix}_optimal_concurrency"] = float(rec.get("optimal_concurrency", 0))
                metrics[f"{prefix}_output_tokens_per_sec"] = float(rec.get("sustained_output_tokens_per_second", 0.0))
                metrics[f"{prefix}_requests_per_sec"] = float(rec.get("sustained_requests_per_second", 0.0))
                metrics[f"{prefix}_ttft_ms_p50"] = float(rec.get("ttft_ms_p50", 0.0))
                metrics[f"{prefix}_ttft_ms_p95"] = float(rec.get("ttft_ms_p95", 0.0))
                metrics[f"{prefix}_ttft_ms_p99"] = float(rec.get("ttft_ms_p99", 0.0))
                metrics[f"{prefix}_itl_ms_p50"] = float(rec.get("itl_ms_p50", 0.0))
                metrics[f"{prefix}_itl_ms_p95"] = float(rec.get("itl_ms_p95", 0.0))
                metrics[f"{prefix}_recommended_rpm"] = float(rec.get("recommended_rpm", 0))
                metrics[f"{prefix}_recommended_tpm"] = float(rec.get("recommended_tpm", 0))

            if mlflow.active_run():
                mlflow.log_metrics(metrics)
            elif run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug("Failed logging GuideLLM benchmark metrics to MLflow: %s", e)

    def upload_to_mlflow(
        self,
        git_slug: Optional[str] = None,
        stage: Optional[str] = None,
        multi_repo: bool = False,
        run_id: Optional[str] = None,
    ):
        """Uploads benchmarks.json (and any GuideLLM HTML reports) to MLflow and DefaultAssetLoader."""
        if not self.records:
            return
        slug = git_slug or self.git_slug
        tags = {
            "git_slug": str(slug or "multi-repo"),
            "category": "telemetry",
            "type": "guidellm_benchmarks",
            "stage": str(stage or "Benchmark"),
            "multi_repo": str(multi_repo),
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_file = os.path.join(tmp_dir, "benchmarks.json")
            self.save_to_file(tmp_file)

            try:
                import mlflow
                active = mlflow.active_run()
                target_run = (
                    run_id
                    or (active.info.run_id if active and hasattr(active, "info") else None)
                    or os.environ.get("MLFLOW_RUN_ID")
                )
                if target_run:
                    if active and hasattr(active, "info") and active.info.run_id == target_run:
                        mlflow.set_tags(tags)
                        mlflow.log_artifact(tmp_file, artifact_path="telemetry")
                        for html_p in self.html_report_paths:
                            if os.path.isfile(html_p):
                                mlflow.log_artifact(html_p, artifact_path="telemetry")
                    else:
                        with mlflow.start_run(run_id=target_run, nested=bool(active)):
                            mlflow.set_tags(tags)
                            mlflow.log_artifact(tmp_file, artifact_path="telemetry")
                            for html_p in self.html_report_paths:
                                if os.path.isfile(html_p):
                                    mlflow.log_artifact(html_p, artifact_path="telemetry")
                    self.log_to_mlflow(run_id=target_run)
            except Exception as e:
                logging.debug("MLflow log_artifact failed for benchmarks.json: %s", e)

            try:
                from loaders.default_asset_loader import DefaultAssetLoader
                artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                    git_slug=slug,
                    multi_repo=multi_repo,
                )
                content_str = None
                try:
                    with open(tmp_file, "r", encoding="utf-8") as f:
                        content_str = f.read()
                except Exception:
                    pass
                DefaultAssetLoader().log_results(
                    tmp_file,
                    artifact_path=artifact_path,
                    tags=tags,
                    content=content_str,
                )
            except Exception as e:
                logging.debug("Asset loader upload failed for benchmarks.json: %s", e)

        if self._profile_sweeps_run or stage in (None, "Data Generation", "Calibration"):
            try:
                from utils.duration_tracker import DurationTracker
                dur_tracker = DurationTracker.get_instance(git_slug=slug, git_repo=self.git_repo)
                has_cal = any(r.get("stage") == "Calibration" for r in dur_tracker.records)
                if self._profile_sweeps_run or has_cal:
                    dur_tracker.upload_to_mlflow(
                        git_slug=slug,
                        stage="Calibration",
                        multi_repo=multi_repo,
                        run_id=run_id,
                    )
                    self._profile_sweeps_run = False
            except Exception as e:
                logging.debug("Failed uploading Calibration durations from BenchmarkTracker: %s", e)

    def download_from_mlflow(
        self,
        git_slug: Optional[str] = None,
        multi_repo: bool = False,
        current_stage: Optional[str] = None,
        run_id: Optional[str] = None,
    ):
        """Downloads and merges benchmarks.json from MLflow or DefaultAssetLoader."""
        slug = git_slug or self.git_slug
        target_run = run_id or os.environ.get("MLFLOW_RUN_ID")

        if not target_run:
            try:
                import mlflow
                active = mlflow.active_run()
                if active and hasattr(active, "info"):
                    target_run = getattr(active.info, "run_id", None)
            except Exception:
                pass

        if target_run and target_run not in self._merged_runs:
            try:
                import mlflow
                local_path = mlflow.artifacts.download_artifacts(
                    run_id=target_run, artifact_path="telemetry/benchmarks.json"
                )
                cand = os.path.join(local_path, "benchmarks.json") if os.path.isdir(local_path or "") else local_path
                if cand and os.path.isfile(cand):
                    self.load_and_merge(cand, current_stage=current_stage)
                    self._merged_runs.add(target_run)
                    return
            except Exception as e:
                logging.debug("Failed downloading benchmarks.json from MLflow for run %s: %s", target_run, e)

        if slug or multi_repo:
            try:
                from loaders.default_asset_loader import DefaultAssetLoader
                from loaders.mlflow_asset_loader import MlFlowAssetLoader

                artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                    DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY,
                    git_slug=slug,
                    multi_repo=multi_repo,
                )
                asset_file = f"{artifact_path}/benchmarks.json"
                asset_tags = {
                    "git_slug": str(slug or "multi-repo"),
                    "type": "guidellm_benchmarks",
                }
                loader = DefaultAssetLoader()
                with tempfile.TemporaryDirectory() as tmp_dir:
                    loaded = False
                    try:
                        content = loader.download(
                            asset_file,
                            download_dir=tmp_dir,
                            experiment_name=MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT,
                            asset_tags=asset_tags,
                        )
                        if isinstance(content, dict) and ("records" in content or "recommendations" in content):
                            cand = os.path.join(tmp_dir, "benchmarks.json")
                            if not os.path.isfile(cand):
                                with open(cand, "w", encoding="utf-8") as f:
                                    json.dump(content, f)
                            self.load_and_merge(cand, current_stage=current_stage)
                            loaded = True
                        elif isinstance(content, str) and content.strip():
                            cand = os.path.join(tmp_dir, "benchmarks.json")
                            if not os.path.isfile(cand):
                                with open(cand, "w", encoding="utf-8") as f:
                                    f.write(content)
                            self.load_and_merge(cand, current_stage=current_stage)
                            loaded = True
                    except Exception as e:
                        logging.debug("DefaultAssetLoader.download failed for benchmarks.json: %s", e)

                    if not loaded:
                        cand = os.path.join(tmp_dir, "benchmarks.json")
                        if not os.path.isfile(cand) and hasattr(loader, "download_dir"):
                            try:
                                loader.download_dir(
                                    artifact_path,
                                    tmp_dir,
                                    experiment_name=MlFlowAssetLoader.RESULT_ASSET_EXPERIMENT,
                                    asset_tags=asset_tags,
                                )
                            except TypeError:
                                loader.download_dir(artifact_path, tmp_dir)
                        if os.path.isfile(cand):
                            self.load_and_merge(cand, current_stage=current_stage)
            except Exception as e:
                logging.debug("Asset loader download failed for benchmarks.json: %s", e)

    def format_summary(self) -> str:
        """Returns an ASCII table of GuideLLM inference benchmark results and recommended settings."""
        title = "LLM INFERENCE BENCHMARK & CONCURRENCY CALIBRATION (GUIDELLM)"
        if not self.records:
            table_w = 88
            divider_eq = "=" * table_w
            return "\n".join([
                f"{title:^{table_w}}",
                divider_eq,
                " No GuideLLM benchmark records captured.",
                divider_eq,
            ])

        header = (
            f" {'Role':<13}{'Workload':<11}{'Conc':<6}{'RPS':<8}{'Out tok/s':<12}"
            f"{'TTFT p50/p95 (ms)':<20}{'ITL p50/p95 (ms)':<18}{'Err %':<7}"
        )
        table_w = max(96, len(header.rstrip()))
        divider_eq = "=" * table_w
        divider_dash = "-" * table_w

        lines = [
            f"{title:^{table_w}}",
            divider_eq,
            header,
            divider_dash,
        ]

        for r in sorted(self.records, key=lambda x: (x.get("role", ""), x.get("workload", ""), x.get("concurrency", 1))):
            ttft_str = f"{r.get('ttft_ms_p50', 0.0):.0f} / {r.get('ttft_ms_p95', 0.0):.0f}"
            itl_str = f"{r.get('itl_ms_p50', 0.0):.1f} / {r.get('itl_ms_p95', 0.0):.1f}"
            err_pct = f"{r.get('error_rate', 0.0) * 100.0:.1f}%"
            lines.append(
                f" {r.get('role', 'graphrag'):<13}"
                f"{r.get('workload', 'indexing'):<11}"
                f"{r.get('concurrency', 1):<6}"
                f"{r.get('requests_per_second', 0.0):<8.2f}"
                f"{r.get('output_tokens_per_second', 0.0):<12.1f}"
                f"{ttft_str:<20}"
                f"{itl_str:<18}"
                f"{err_pct:<7}"
            )

        if self.recommendations:
            lines.append(divider_dash)
            for role, rec in sorted(self.recommendations.items()):
                lines.append(
                    f" [RECOMMENDED {role.upper()}] Model: {rec.get('model_id')} | "
                    f"Optimal Concurrency: {rec.get('optimal_concurrency')} "
                    f"(SDG: {rec.get('recommended_sdg_concurrency')}) | "
                    f"RPM: {rec.get('recommended_rpm'):,} | TPM: {rec.get('recommended_tpm'):,}"
                )

        lines.append(divider_eq)
        return "\n".join(lines)

    def format_markdown_section(self) -> str:
        """Returns a Markdown section summarizing GuideLLM endpoint performance for migration_report.md."""
        if not self.records:
            return ""

        lines = [
            "\n### LLM Inference Server Benchmark & Calibration (GuideLLM)\n",
            "Measured inference server latency (**TTFT** / **ITL**), token throughput, and auto-tuned concurrency limits:\n",
            f"```\n{self.format_summary()}\n```",
        ]
        return "\n".join(lines) + "\n"

    def generate_markdown_summary(self) -> str:
        """Alias for format_markdown_section used by Stage 3 Analysis migration report generator."""
        return self.format_markdown_section()


def _cli_main() -> int:
    default_workload = _clean_env("GUIDELLM_WORKLOAD") or _clean_env("GUIDELLM_PROFILE") or "indexing"
    if default_workload.lower() not in ("precheck", "sdg", "indexing", "analysis", "all"):
        default_workload = "indexing"

    default_profile = _clean_env("GUIDELLM_PROFILE", "")
    if default_profile.lower() in ("precheck", "sdg", "indexing", "analysis", "all"):
        default_profile = ""

    parser = argparse.ArgumentParser(description="Run GuideLLM benchmark and compute AUGUR concurrency settings")
    parser.add_argument("--role", default=_clean_env("GUIDELLM_TARGET_ROLE", "graphrag"),
                        help="Target LLM role: graphrag | ground_truth | judge | all")
    parser.add_argument("--workload", default=default_workload,
                        help="AUGUR workload profile: precheck | sdg | indexing | analysis | all")
    parser.add_argument("--profile", default=default_profile,
                        help="GuideLLM rate profile: concurrent | sweep | throughput | synchronous")
    parser.add_argument("--rates", default=_clean_env("GUIDELLM_CONCURRENCY_RATES") or _clean_env("GUIDELLM_RATES"),
                        help="Comma-separated concurrency levels (e.g. '1,4,8,16')")
    parser.add_argument("--max-seconds", type=int, default=int(_clean_env("GUIDELLM_MAX_SECONDS", "0") or 0),
                        help="Max duration per benchmark rate in seconds")
    parser.add_argument("--output-file", default=_clean_env("GUIDELLM_OUTPUT_FILE", "/tmp/augur_guidellm_benchmarks.json"),
                        help="Path to write normalized benchmarks.json")
    parser.add_argument("--env-export-file", default="",
                        help="Optional path to write shell 'export KEY=VAL' auto-tuned variables")
    parser.add_argument("--upload-mlflow", action="store_true",
                        help="Upload benchmark telemetry to MLflow if MLFLOW_TRACKING_URI is configured")
    parser.add_argument("--enforce-slo", action="store_true",
                        help="Exit non-zero if throughput falls below GUIDELLM_MIN_TOK_PER_SEC or error_rate > 0")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(asctime)s - %(message)s")

    git_repo = _clean_env("GIT_REPO", "")
    git_branch = _clean_env("GIT_BRANCH", "main")
    git_slug = None
    if git_repo:
        try:
            from pipelines.base.data_generation import generate_git_slug
            git_slug = generate_git_slug(git_repo, git_branch)
        except Exception:
            pass

    tracker = BenchmarkTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
    roles = ["graphrag", "ground_truth", "judge"] if args.role == "all" else [args.role]
    workloads = ["sdg", "indexing", "analysis"] if args.workload == "all" else [args.workload]

    rate_profile = args.profile if args.profile not in ("precheck", "sdg", "indexing", "analysis", "all") else None

    for r in roles:
        for w in workloads:
            tracker.run_benchmark(
                role=r,
                workload=w,
                profile=rate_profile or None,
                rates_str=args.rates or None,
                max_seconds=args.max_seconds if args.max_seconds > 0 else None,
            )

    if args.output_file:
        tracker.save_to_file(args.output_file)
        logging.info("Saved GuideLLM benchmark telemetry to %s", args.output_file)

    print("\n" + tracker.format_summary() + "\n", flush=True)

    rec_limits = tracker.get_recommended_graphrag_limits("graphrag")
    if rec_limits and args.env_export_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.env_export_file)), exist_ok=True)
        with open(args.env_export_file, "w", encoding="utf-8") as ef:
            ef.write(f"export GRAPHRAG_CONCURRENT_REQUESTS={rec_limits['concurrent_requests']}\n")
            ef.write(f"export GRAPHRAG_REQUESTS_PER_MINUTE={rec_limits['requests_per_minute']}\n")
            ef.write(f"export GRAPHRAG_TOKENS_PER_MINUTE={rec_limits['tokens_per_minute']}\n")
            ef.write(f"export SDG_LLM_MAX_CONCURRENCY={rec_limits['sdg_max_concurrency']}\n")

    if args.upload_mlflow and _clean_env("MLFLOW_TRACKING_URI"):
        tracker.upload_to_mlflow(git_slug=git_slug, stage="Benchmark")

    if args.enforce_slo:
        min_tok_s = float(_clean_env("GUIDELLM_MIN_TOK_PER_SEC", "5.0"))
        rec = tracker.recommendations.get(roles[0])
        if not rec:
            logging.error("SLO check failed: No benchmark records produced for role '%s'.", roles[0])
            return 1
        if rec.get("error_rate", 0.0) >= 1.0:
            logging.error("SLO check failed: 100%% request error rate on role '%s'.", roles[0])
            return 1
        if rec.get("sustained_output_tokens_per_second", 0.0) < min_tok_s:
            logging.error(
                "SLO check failed: Output throughput %.2f tok/s is below GUIDELLM_MIN_TOK_PER_SEC=%.2f.",
                rec.get("sustained_output_tokens_per_second", 0.0), min_tok_s,
            )
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
