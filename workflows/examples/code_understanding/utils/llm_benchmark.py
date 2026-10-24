"""GuideLLM benchmarking utility for discovering optimal LLM concurrency settings."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

BENCHMARK_RESULT_FILENAME = "benchmark_results.json"
DEFAULT_CONCURRENCY_LEVELS = (1, 2, 4, 8, 16, 32)
DEFAULT_MAX_SECONDS = 15
DEFAULT_MAX_REQUESTS = 16
DEFAULT_PROMPT_TOKENS = 512
DEFAULT_OUTPUT_TOKENS = 256
DEFAULT_FALLBACK_CONCURRENCY = 10


def is_benchmark_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """Return True if RUN_LLM_BENCHMARK is enabled (defaults to True)."""
    source = env if env is not None else os.environ
    val = str(source.get("RUN_LLM_BENCHMARK", "true")).strip().lower()
    return val not in ("0", "false", "no", "off", "skip")


def is_benchmark_skipped(env: Optional[Dict[str, str]] = None) -> bool:
    """Return True if RUN_LLM_BENCHMARK is set to a falsy value."""
    return not is_benchmark_enabled(env)


def parse_concurrency_levels(raw: Optional[str] = None) -> List[int]:
    """Parse comma-separated concurrency levels into a sorted list of positive ints."""
    if not raw:
        raw = os.environ.get("GUIDELLM_CONCURRENCY_LEVELS") or os.environ.get("GUIDELLM_RATES") or ""
    if not raw.strip():
        return list(DEFAULT_CONCURRENCY_LEVELS)

    levels: List[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(float(part))
            if value > 0 and value not in levels:
                levels.append(value)
        except ValueError:
            logger.warning("Ignoring invalid concurrency level '%s'", part)

    return sorted(levels) if levels else list(DEFAULT_CONCURRENCY_LEVELS)


def resolve_llm_endpoint(
    api_base: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Resolve target API base URL, model ID, and API key from arguments or environment."""
    source = env if env is not None else os.environ
    resolved_base = (
        api_base
        or source.get("GUIDELLM_TARGET")
        or source.get("GRAPHRAG_LLM_API_BASE")
        or source.get("CODE_LLM_API_BASE")
        or ""
    ).strip().rstrip("/")
    resolved_model = (
        model
        or source.get("GUIDELLM_MODEL")
        or source.get("GRAPHRAG_LLM_ID")
        or source.get("CODE_LLM_ID")
        or ""
    ).strip()
    resolved_key = (
        api_key
        if api_key is not None
        else (
            source.get("GUIDELLM_API_KEY")
            or source.get("GRAPHRAG_LLM_TOKEN")
            or source.get("CODE_LLM_TOKEN")
            or ""
        )
    ).strip()
    return {
        "api_base": resolved_base,
        "model": resolved_model,
        "api_key": resolved_key,
    }


def _extract_stat_mean(stat_obj: Any, default: float = 0.0) -> float:
    """Extract a mean float from a GuideLLM metric value or nested distribution dict."""
    if isinstance(stat_obj, (int, float)):
        return float(stat_obj)
    if isinstance(stat_obj, dict):
        for key in ("mean", "average", "median", "p50", "total"):
            if key in stat_obj and isinstance(stat_obj[key], (int, float)):
                return float(stat_obj[key])
        for subkey in ("successful", "total", "all"):
            sub = stat_obj.get(subkey)
            if isinstance(sub, (int, float)):
                return float(sub)
            if isinstance(sub, dict):
                val = _extract_stat_mean(sub, default= -1.0)
                if val >= 0.0:
                    return val
    return default


def parse_guidellm_json(data: Any, requested_levels: Optional[Sequence[int]] = None) -> List[Dict[str, Any]]:
    """Parse GuideLLM JSON report payload into normalized per-concurrency level records."""
    if isinstance(data, str):
        data = json.loads(data)

    benchmarks: List[Any] = []
    if isinstance(data, dict):
        if isinstance(data.get("benchmarks"), list):
            benchmarks = data["benchmarks"]
        elif isinstance(data.get("runs"), list):
            benchmarks = data["runs"]
        elif "metrics" in data or "stats" in data:
            benchmarks = [data]
    elif isinstance(data, list):
        benchmarks = data

    parsed_levels: List[Dict[str, Any]] = []
    for idx, entry in enumerate(benchmarks):
        if not isinstance(entry, dict):
            continue

        args = entry.get("benchmark_args") or entry.get("args") or entry.get("profile") or {}
        metrics = entry.get("metrics") or entry.get("stats") or entry

        concurrency = None
        for key in ("request_concurrency", "concurrency", "streams", "max_concurrency", "rate"):
            val = args.get(key) if isinstance(args, dict) else None
            if val is None:
                val = entry.get(key)
            if isinstance(val, list) and val:
                val = val[0]
            if isinstance(val, (int, float)) and val > 0:
                concurrency = int(round(val))
                break

        if concurrency is None and isinstance(args, dict):
            profile = args.get("profile")
            if isinstance(profile, dict):
                streams = profile.get("streams") or profile.get("rate")
                if isinstance(streams, list) and streams:
                    streams = streams[0]
                if isinstance(streams, (int, float)) and streams > 0:
                    concurrency = int(round(streams))

        if concurrency is None and requested_levels and idx < len(requested_levels):
            concurrency = int(requested_levels[idx])

        if concurrency is None or concurrency <= 0:
            continue

        req_totals = metrics.get("request_totals") or entry.get("request_totals") or {}
        successful = int(
            metrics.get("successful_requests")
            or metrics.get("completed_requests")
            or req_totals.get("successful")
            or req_totals.get("completed")
            or 0
        )
        failed = int(
            metrics.get("failed_requests")
            or metrics.get("errored_requests")
            or req_totals.get("errored")
            or req_totals.get("failed")
            or 0
        )
        total_reqs = successful + failed
        error_rate = (failed / total_reqs) if total_reqs > 0 else 0.0

        rps = _extract_stat_mean(
            metrics.get("requests_per_second")
            or metrics.get("request_throughput")
            or metrics.get("completed_requests_per_second"),
            0.0,
        )
        out_tps = _extract_stat_mean(
            metrics.get("output_tokens_per_second")
            or metrics.get("output_token_throughput")
            or metrics.get("tokens_per_second"),
            0.0,
        )
        total_tps = _extract_stat_mean(
            metrics.get("tokens_per_second")
            or metrics.get("total_tokens_per_second"),
            out_tps,
        )
        latency_mean_s = _extract_stat_mean(
            metrics.get("request_latency")
            or metrics.get("request_duration")
            or metrics.get("mean_latency")
            or metrics.get("latency"),
            0.0,
        )
        ttft_ms = _extract_stat_mean(
            metrics.get("time_to_first_token_ms")
            or metrics.get("ttft_ms")
            or metrics.get("time_to_first_token"),
            0.0,
        )
        itl_ms = _extract_stat_mean(
            metrics.get("inter_token_latency_ms")
            or metrics.get("itl_ms")
            or metrics.get("inter_token_latency"),
            0.0,
        )

        # If successful wasn't explicitly set in metrics but rps > 0 or out_tps > 0, mark at least 1
        if successful == 0 and failed == 0 and (rps > 0 or out_tps > 0):
            successful = 1

        parsed_levels.append({
            "concurrency": concurrency,
            "requests_per_second": round(rps, 3),
            "output_tokens_per_second": round(out_tps, 3),
            "tokens_per_second": round(total_tps, 3),
            "latency_mean_s": round(latency_mean_s, 4),
            "ttft_ms": round(ttft_ms, 2),
            "itl_ms": round(itl_ms, 2),
            "successful_requests": successful,
            "failed_requests": failed,
            "error_rate": round(error_rate, 4),
        })

    parsed_levels.sort(key=lambda item: item["concurrency"])
    return parsed_levels


def select_best_concurrency(
    levels: Sequence[Dict[str, Any]],
    default_concurrency: int = DEFAULT_FALLBACK_CONCURRENCY,
    max_error_rate: float = 0.10,
) -> int:
    """Select the optimal concurrency setting from benchmark level metrics.

    Selection strategy:
    1. Filter to levels with successful requests > 0 and error_rate <= max_error_rate.
       (If no level meets max_error_rate, fall back to levels with the minimum error_rate).
    2. Rank candidates by effective throughput (`output_tokens_per_second`, falling back to
       `requests_per_second`).
    3. Find the peak throughput and select the lowest concurrency level that achieves at least
       95% of peak throughput (avoiding unnecessary over-concurrency when throughput plateaus).
    """
    valid = [
        lvl for lvl in levels
        if int(lvl.get("successful_requests", 0)) > 0
        and (float(lvl.get("output_tokens_per_second", 0.0)) > 0 or float(lvl.get("requests_per_second", 0.0)) > 0)
    ]
    if not valid:
        return default_concurrency

    low_error = [lvl for lvl in valid if float(lvl.get("error_rate", 0.0)) <= max_error_rate]
    if not low_error:
        min_err = min(float(lvl.get("error_rate", 1.0)) for lvl in valid)
        low_error = [lvl for lvl in valid if float(lvl.get("error_rate", 1.0)) <= min_err + 1e-6]

    def _score(item: Dict[str, Any]) -> float:
        out_tps = float(item.get("output_tokens_per_second", 0.0))
        rps = float(item.get("requests_per_second", 0.0))
        err_penalty = 1.0 - float(item.get("error_rate", 0.0))
        primary = out_tps if out_tps > 0 else rps
        return primary * max(0.0, err_penalty)

    peak_score = max(_score(lvl) for lvl in low_error)
    if peak_score <= 0:
        return default_concurrency

    # Choose the smallest concurrency that reaches >= 95% of peak effective throughput
    threshold = peak_score * 0.95
    near_peak = [lvl for lvl in low_error if _score(lvl) >= threshold]
    near_peak.sort(key=lambda item: (int(item["concurrency"]), -_score(item)))
    return int(near_peak[0]["concurrency"])


def _find_guidellm_executable() -> Optional[List[str]]:
    """Locate the guidellm CLI executable or module invocation."""
    exe = shutil.which("guidellm")
    if exe:
        return [exe]

    for venv_dir in (".venv", "venv", "/opt/app-root"):
        for bin_dir in ("bin", "Scripts"):
            candidate = Path(venv_dir) / bin_dir / "guidellm"
            if candidate.is_file():
                return [str(candidate)]

    try:
        import importlib.util
        if importlib.util.find_spec("guidellm") is not None:
            return [sys.executable, "-m", "guidellm"]
    except Exception:
        pass

    return None


def _detect_guidellm_subcommand(base_cmd: List[str], env: Dict[str, str]) -> str:
    """Detect whether the installed GuideLLM CLI uses `benchmark run`, `benchmark`, or `run`."""
    try:
        proc = subprocess.run(
            [*base_cmd, "--help"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            check=False,
        )
        help_text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "benchmark" in help_text:
            bench_proc = subprocess.run(
                [*base_cmd, "benchmark", "--help"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
                check=False,
            )
            bench_help = (bench_proc.stdout or "") + "\n" + (bench_proc.stderr or "")
            if "--target" not in bench_help and "run" in bench_help:
                return "benchmark_run"
            return "benchmark"
        if "run" in help_text:
            return "run"
    except Exception as exc:
        logger.debug("Could not inspect guidellm --help: %s", exc)
    return "benchmark"


def _run_guidellm_cli_for_level(
    base_cmd: List[str],
    subcommand: str,
    api_base: str,
    model: str,
    api_key: str,
    concurrency: int,
    max_seconds: int,
    max_requests: int,
    prompt_tokens: int,
    output_tokens: int,
    output_path: str,
    verify_tls: bool,
) -> Optional[Dict[str, Any]]:
    """Execute GuideLLM CLI for a single concurrency level and parse its JSON output."""
    env = dict(os.environ)
    tmp_root = tempfile.gettempdir()
    if not os.access(env.get("HOME", "/"), os.W_OK):
        env["HOME"] = tmp_root
    env.setdefault("HF_HOME", os.path.join(tmp_root, ".cache", "huggingface"))
    if api_key:
        env["OPENAI_API_KEY"] = api_key
        env["GUIDELLM__OPENAI__API_KEY"] = api_key
    if not verify_tls:
        env["GUIDELLM__OPENAI__VERIFY"] = "false"
        env["PYTHONHTTPSVERIFY"] = "0"
        env["CURL_CA_BUNDLE"] = ""
        env["REQUESTS_CA_BUNDLE"] = ""

    data_arg = f"prompt_tokens={prompt_tokens},output_tokens={output_tokens}"
    if subcommand == "run":
        cmd = [
            *base_cmd,
            "run",
            "--backend",
            f"kind=openai_http,target={api_base}",
            "--model",
            model,
            "--profile",
            "kind=concurrent",
            "--override",
            f"profile.streams={concurrency}",
            "--data",
            f"kind=synthetic_text,{data_arg}",
            "--constraint",
            f"kind=max_duration,seconds={max_seconds}",
            "--output",
            f"kind=json,path={output_path}",
        ]
    else:
        sub_args = ["benchmark", "run"] if subcommand == "benchmark_run" else ["benchmark"]
        cmd = [
            *base_cmd,
            *sub_args,
            "--target",
            api_base,
            "--model",
            model,
            "--rate-type",
            "concurrent",
            "--rate",
            str(concurrency),
            "--max-seconds",
            str(max_seconds),
            "--max-requests",
            str(max_requests),
            "--data",
            data_arg,
            "--output-path",
            output_path,
        ]

    logger.info("Running GuideLLM benchmark at concurrency=%d ...", concurrency)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(60, max_seconds * 4),
        env=env,
        check=False,
    )

    if not os.path.exists(output_path):
        # Some GuideLLM versions write benchmarks.json inside a directory or current dir
        alt_path = os.path.join(os.path.dirname(output_path), "benchmarks.json")
        if os.path.exists(alt_path):
            output_path = alt_path

    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        parsed = parse_guidellm_json(payload, requested_levels=[concurrency])
        if parsed:
            return parsed[0]

    logger.debug(
        "GuideLLM CLI exited with code %s for concurrency=%d. stderr=%s",
        proc.returncode,
        concurrency,
        (proc.stderr or "")[:500],
    )
    return None


async def _probe_single_request(
    url: str,
    headers: Dict[str, str],
    body_bytes: bytes,
    ssl_ctx: ssl.SSLContext,
    timeout: float,
) -> Dict[str, Any]:
    """Send one chat completion request in a worker thread and measure latency & token throughput."""
    start = time.perf_counter()

    def _do_request():
        req = urllib.request.Request(url, data=body_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ssl_ctx, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)

    try:
        payload = await asyncio.to_thread(_do_request)
        elapsed = max(1e-6, time.perf_counter() - start)
        usage = payload.get("usage") or {}
        completion_tokens = int(usage.get("completion_tokens") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        if completion_tokens == 0:
            choices = payload.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                content = str(msg.get("content") or msg.get("reasoning_content") or "")
                completion_tokens = max(1, len(content) // 4)
        return {
            "ok": True,
            "elapsed": elapsed,
            "completion_tokens": completion_tokens,
            "prompt_tokens": prompt_tokens,
        }
    except Exception as exc:
        elapsed = max(1e-6, time.perf_counter() - start)
        logger.debug("Probe request failed after %.2fs: %s", elapsed, exc)
        return {
            "ok": False,
            "elapsed": elapsed,
            "completion_tokens": 0,
            "prompt_tokens": 0,
            "error": str(exc),
        }


async def _run_async_probe_level(
    api_base: str,
    model: str,
    api_key: str,
    concurrency: int,
    max_seconds: int,
    max_requests: int,
    output_tokens: int,
    verify_tls: bool,
) -> Dict[str, Any]:
    """Fallback concurrent benchmark probe against an OpenAI-compatible chat completions endpoint."""
    base = api_base.rstrip("/")
    if not base.endswith("/chat/completions"):
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/v1/chat/completions"
    else:
        url = base

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    prompt_text = (
        "Summarize the purpose, classes, functions, and dependencies of a Python module "
        "in concise JSON format with keys: purpose, classes, functions."
    )
    body_bytes = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "max_tokens": min(output_tokens, 128),
        "temperature": 0,
    }).encode("utf-8")

    ssl_ctx = ssl.create_default_context()
    if not verify_tls:
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    total_to_send = max(concurrency, min(max_requests, concurrency * 2))
    sem = asyncio.Semaphore(concurrency)
    results: List[Dict[str, Any]] = []
    level_start = time.perf_counter()

    async def _worker():
        async with sem:
            if time.perf_counter() - level_start > max_seconds:
                return
            res = await _probe_single_request(
                url=url,
                headers=headers,
                body_bytes=body_bytes,
                ssl_ctx=ssl_ctx,
                timeout=max(10.0, float(max_seconds)),
            )
            results.append(res)

    await asyncio.gather(*[_worker() for _ in range(total_to_send)])
    wall_time = max(1e-6, time.perf_counter() - level_start)

    ok_results = [r for r in results if r.get("ok")]
    failed_count = len(results) - len(ok_results)
    successful_count = len(ok_results)
    total_count = len(results)
    error_rate = (failed_count / total_count) if total_count > 0 else 1.0

    total_out_tokens = sum(int(r.get("completion_tokens", 0)) for r in ok_results)
    total_all_tokens = total_out_tokens + sum(int(r.get("prompt_tokens", 0)) for r in ok_results)
    mean_latency = (
        sum(float(r.get("elapsed", 0.0)) for r in ok_results) / successful_count
        if successful_count > 0
        else 0.0
    )

    rps = successful_count / wall_time if successful_count > 0 else 0.0
    out_tps = total_out_tokens / wall_time if successful_count > 0 else 0.0
    tot_tps = total_all_tokens / wall_time if successful_count > 0 else 0.0

    return {
        "concurrency": concurrency,
        "requests_per_second": round(rps, 3),
        "output_tokens_per_second": round(out_tps, 3),
        "tokens_per_second": round(tot_tps, 3),
        "latency_mean_s": round(mean_latency, 4),
        "ttft_ms": 0.0,
        "itl_ms": 0.0,
        "successful_requests": successful_count,
        "failed_requests": failed_count,
        "error_rate": round(error_rate, 4),
    }


def format_benchmark_summary(result: Dict[str, Any]) -> str:
    """Format a human-readable ASCII summary table of the GuideLLM concurrency benchmark."""
    lines = [
        "=" * 86,
        "GuideLLM Concurrency Benchmark Summary",
        "=" * 86,
        f"  Target Endpoint : {result.get('api_base') or '<unset>'}",
        f"  Model           : {result.get('model') or '<unset>'}",
        f"  Runner          : {result.get('runner', 'guidellm')}",
        f"  Total Duration  : {float(result.get('duration_seconds', 0.0)):.2f}s",
    ]
    if result.get("skipped"):
        lines.append(f"  Status          : Skipped ({result.get('reason', 'RUN_LLM_BENCHMARK=false')})")
    else:
        lines.extend([
            "-" * 86,
            f"  {'Concurrency':>11} | {'Out Tok/s':>11} | {'Req/s':>9} | {'Mean Lat (s)':>12} | {'Success':>7} | {'Errors':>6} | {'Err %':>6}",
            "-" * 86,
        ])
        best_c = int(result.get("best_concurrency", DEFAULT_FALLBACK_CONCURRENCY))
        for lvl in result.get("levels", []):
            c = int(lvl.get("concurrency", 0))
            marker = "*" if c == best_c else " "
            lines.append(
                f" {marker}{c:>10} | "
                f"{float(lvl.get('output_tokens_per_second', 0.0)):>11.2f} | "
                f"{float(lvl.get('requests_per_second', 0.0)):>9.2f} | "
                f"{float(lvl.get('latency_mean_s', 0.0)):>12.3f} | "
                f"{int(lvl.get('successful_requests', 0)):>7} | "
                f"{int(lvl.get('failed_requests', 0)):>6} | "
                f"{float(lvl.get('error_rate', 0.0)) * 100:>5.1f}%"
            )
    lines.extend([
        "-" * 86,
        f"  Recommended Concurrency : {result.get('best_concurrency')}",
        f"    SDG_LLM_MAX_CONCURRENCY={result.get('best_concurrency')}",
        f"    GRAPHRAG_CONCURRENT_REQUESTS={result.get('best_concurrency')}",
        "=" * 86,
    ])
    return "\n".join(lines)


format_benchmark_report = format_benchmark_summary


def apply_best_concurrency(best_concurrency: int) -> int:
    """Apply the benchmarked concurrency to process environment variables for Data Generation & Indexing."""
    concurrency_val = max(1, int(best_concurrency))
    os.environ["SDG_LLM_MAX_CONCURRENCY"] = str(concurrency_val)
    os.environ["GRAPHRAG_CONCURRENT_REQUESTS"] = str(concurrency_val)
    logger.info(
        "Applied optimal LLM concurrency setting: SDG_LLM_MAX_CONCURRENCY=%d, GRAPHRAG_CONCURRENT_REQUESTS=%d",
        concurrency_val,
        concurrency_val,
    )
    return concurrency_val


def save_benchmark_result(result: Dict[str, Any], directory: str) -> Optional[str]:
    """Save benchmark_results.json into directory."""
    if not directory:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        out_path = os.path.join(directory, BENCHMARK_RESULT_FILENAME)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        return out_path
    except Exception as exc:
        logger.debug("Failed to save benchmark result to %s: %s", directory, exc)
        return None


def load_benchmark_result(directory_or_file: str) -> Optional[Dict[str, Any]]:
    """Load benchmark_results.json from a file path or directory if it exists."""
    if not directory_or_file:
        return None
    path = (
        directory_or_file
        if directory_or_file.endswith(".json")
        else os.path.join(directory_or_file, BENCHMARK_RESULT_FILENAME)
    )
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("Failed to load benchmark result from %s: %s", path, exc)
        return None


def run_guidellm_benchmark(
    api_base: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    concurrency_levels: Optional[Sequence[int]] = None,
    max_seconds: Optional[int] = None,
    max_requests: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    apply_env: bool = False,
    respect_skip_env: bool = True,
) -> Dict[str, Any]:
    """Run GuideLLM concurrency sweep against the configured LLM endpoint and find the best concurrency.

    Args:
        api_base: Override LLM base URL (defaults to GRAPHRAG_LLM_API_BASE).
        model: Override LLM model ID (defaults to GRAPHRAG_LLM_ID).
        api_key: Override LLM API token (defaults to GRAPHRAG_LLM_TOKEN).
        concurrency_levels: Sequence of concurrency values to test.
        max_seconds: Max duration in seconds per concurrency level.
        max_requests: Max requests per concurrency level.
        prompt_tokens: Synthetic prompt token length for GuideLLM.
        output_tokens: Synthetic output token length for GuideLLM.
        apply_env: If True, update os.environ['SDG_LLM_MAX_CONCURRENCY'] and
                   os.environ['GRAPHRAG_CONCURRENT_REQUESTS'] with the best concurrency.
        respect_skip_env: If True and RUN_LLM_BENCHMARK=false, skip benchmarking and
                          return the current/default concurrency.
    """
    fallback_concurrency = int(
        os.environ.get("SDG_LLM_MAX_CONCURRENCY")
        or os.environ.get("GRAPHRAG_CONCURRENT_REQUESTS")
        or DEFAULT_FALLBACK_CONCURRENCY
    )
    endpoint = resolve_llm_endpoint(api_base=api_base, model=model, api_key=api_key)

    if respect_skip_env and is_benchmark_skipped():
        result = {
            "best_concurrency": fallback_concurrency,
            "api_base": endpoint["api_base"],
            "model": endpoint["model"],
            "runner": "skipped",
            "skipped": True,
            "reason": "RUN_LLM_BENCHMARK is false",
            "levels": [],
            "duration_seconds": 0.0,
        }
        if apply_env:
            apply_best_concurrency(fallback_concurrency)
        return result

    if not endpoint["api_base"] or not endpoint["model"]:
        logger.info(
            "LLM endpoint or model is not configured (api_base=%r, model=%r). "
            "Using fallback concurrency=%d.",
            endpoint["api_base"],
            endpoint["model"],
            fallback_concurrency,
        )
        result = {
            "best_concurrency": fallback_concurrency,
            "api_base": endpoint["api_base"],
            "model": endpoint["model"],
            "runner": "fallback",
            "skipped": True,
            "reason": "Missing GRAPHRAG_LLM_API_BASE or GRAPHRAG_LLM_ID",
            "levels": [],
            "duration_seconds": 0.0,
        }
        if apply_env:
            apply_best_concurrency(fallback_concurrency)
        return result

    levels_to_test = (
        [int(c) for c in concurrency_levels if int(c) > 0]
        if concurrency_levels
        else parse_concurrency_levels()
    )
    resolved_max_seconds = int(
        max_seconds if max_seconds is not None else os.environ.get("GUIDELLM_MAX_SECONDS", DEFAULT_MAX_SECONDS)
    )
    resolved_max_requests = int(
        max_requests if max_requests is not None else os.environ.get("GUIDELLM_MAX_REQUESTS", DEFAULT_MAX_REQUESTS)
    )
    resolved_prompt_tokens = int(
        prompt_tokens if prompt_tokens is not None else os.environ.get("GUIDELLM_PROMPT_TOKENS", DEFAULT_PROMPT_TOKENS)
    )
    resolved_output_tokens = int(
        output_tokens if output_tokens is not None else os.environ.get("GUIDELLM_OUTPUT_TOKENS", DEFAULT_OUTPUT_TOKENS)
    )
    verify_tls = str(os.environ.get("ENABLE_TLS_VERIFICATION", "false")).strip().lower() in ("1", "true", "yes")

    start_time = time.perf_counter()
    level_metrics: List[Dict[str, Any]] = []
    runner_used = "guidellm"

    guidellm_cmd = _find_guidellm_executable()
    if guidellm_cmd:
        subcommand = _detect_guidellm_subcommand(guidellm_cmd, dict(os.environ))
        with tempfile.TemporaryDirectory(prefix="guidellm_bench_") as tmp_dir:
            for c in levels_to_test:
                out_file = os.path.join(tmp_dir, f"benchmark_c{c}.json")
                try:
                    lvl_res = _run_guidellm_cli_for_level(
                        base_cmd=guidellm_cmd,
                        subcommand=subcommand,
                        api_base=endpoint["api_base"],
                        model=endpoint["model"],
                        api_key=endpoint["api_key"],
                        concurrency=c,
                        max_seconds=resolved_max_seconds,
                        max_requests=resolved_max_requests,
                        prompt_tokens=resolved_prompt_tokens,
                        output_tokens=resolved_output_tokens,
                        output_path=out_file,
                        verify_tls=verify_tls,
                    )
                    if lvl_res:
                        level_metrics.append(lvl_res)
                    elif not level_metrics:
                        break
                except Exception as exc:
                    logger.warning("GuideLLM CLI failed for concurrency=%d: %s", c, exc)
                    if not level_metrics:
                        break

    if not level_metrics:
        runner_used = "async_http_probe"
        logger.info(
            "GuideLLM CLI results unavailable; running concurrent HTTP benchmark probe across %s ...",
            levels_to_test,
        )
        for c in levels_to_test:
            try:
                logger.info("Probing LLM endpoint at concurrency=%d ...", c)
                lvl_res = asyncio.run(
                    _run_async_probe_level(
                        api_base=endpoint["api_base"],
                        model=endpoint["model"],
                        api_key=endpoint["api_key"],
                        concurrency=c,
                        max_seconds=resolved_max_seconds,
                        max_requests=resolved_max_requests,
                        output_tokens=resolved_output_tokens,
                        verify_tls=verify_tls,
                    )
                )
                level_metrics.append(lvl_res)
            except Exception as exc:
                logger.warning("Async benchmark probe failed for concurrency=%d: %s", c, exc)

    total_duration = round(time.perf_counter() - start_time, 3)
    best_concurrency = select_best_concurrency(
        level_metrics,
        default_concurrency=fallback_concurrency,
    )

    result = {
        "best_concurrency": best_concurrency,
        "api_base": endpoint["api_base"],
        "model": endpoint["model"],
        "runner": runner_used,
        "skipped": False,
        "levels": level_metrics,
        "duration_seconds": total_duration,
    }

    summary_text = format_benchmark_summary(result)
    logger.info("\n%s", summary_text)

    if apply_env:
        apply_best_concurrency(best_concurrency)

    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for standalone GuideLLM benchmarking (`make benchmark-llm`)."""
    parser = argparse.ArgumentParser(
        description="Benchmark the configured LLM endpoint with GuideLLM to find the best concurrency setting."
    )
    parser.add_argument("--api-base", default=None, help="Override LLM API base URL")
    parser.add_argument("--model", default=None, help="Override LLM model identifier")
    parser.add_argument("--api-key", default=None, help="Override LLM API key/token")
    parser.add_argument(
        "--concurrency-levels",
        default=None,
        help="Comma-separated concurrency levels to benchmark (default: 1,2,4,8,16,32)",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=None,
        help=f"Max duration in seconds per concurrency level (default: {DEFAULT_MAX_SECONDS})",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help=f"Max requests per concurrency level (default: {DEFAULT_MAX_REQUESTS})",
    )
    parser.add_argument(
        "--output-file",
        default=None,
        help="Optional path to write the JSON benchmark results",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO").upper(),
        format="[%(levelname)s] %(asctime)s - %(name)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    levels = parse_concurrency_levels(args.concurrency_levels) if args.concurrency_levels else None
    result = run_guidellm_benchmark(
        api_base=args.api_base,
        model=args.model,
        api_key=args.api_key,
        concurrency_levels=levels,
        max_seconds=args.max_seconds,
        max_requests=args.max_requests,
        apply_env=False,
        respect_skip_env=False,
    )

    if args.output_file:
        out_dir = os.path.dirname(os.path.abspath(args.output_file))
        os.makedirs(out_dir, exist_ok=True)
        with open(args.output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    print(format_benchmark_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
