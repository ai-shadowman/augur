# AUGUR GuideLLM Integration: Dynamic LLM Concurrency Benchmarking & Auto-Tuning
*Comprehensive guide for benchmarking OpenAI-compatible / vLLM inference endpoints and automatically tuning pipeline concurrency.*

---

## Table of Contents
1. [Overview & Purpose](#1-overview--purpose)
2. [Execution Modes](#2-execution-modes)
   - [Mode 1: Standalone OpenShift Job (`make benchmark-llm`)](#mode-1-standalone-openshift-job-make-benchmark-llm)
   - [Mode 2: Pre-Pipeline KFP Step (`benchmark_llm_op`)](#mode-2-pre-pipeline-kfp-step-benchmark_llm_op)
3. [Core Architecture & Selection Algorithm](#3-core-architecture--selection-algorithm)
   - [Endpoint Resolution](#endpoint-resolution)
   - [Dual-Runner Architecture (GuideLLM CLI + Async HTTP Fallback)](#dual-runner-architecture-guidellm-cli--async-http-fallback)
   - [Optimal Concurrency Selection (`select_best_concurrency`)](#optimal-concurrency-selection-select_best_concurrency)
4. [End-to-End Pipeline Propagation & Safeguards](#4-end-to-end-pipeline-propagation--safeguards)
   - [Pre-Pipeline Benchmark Step](#pre-pipeline-benchmark-step)
   - [Stage 1: Synthetic Data Generation (`SDG_LLM_MAX_CONCURRENCY`)](#stage-1-synthetic-data-generation-sdg_llm_max_concurrency)
   - [Stage 2: GraphRAG Indexing (`GRAPHRAG_CONCURRENT_REQUESTS`)](#stage-2-graphrag-indexing-graphrag_concurrent_requests)
   - [Stage 3: Analysis & Migration Report Duration Table](#stage-3-analysis--migration-report-duration-table)
   - [Telemetry Artifact Exclusion Safeguards](#telemetry-artifact-exclusion-safeguards)
5. [Configuration & Environment Variables](#5-configuration--environment-variables)
6. [Artifact Formats & Console Outputs](#6-artifact-formats--console-outputs)
   - [Console ASCII Benchmark Summary](#console-ascii-benchmark-summary)
   - [`benchmark_results.json` Schema](#benchmark_resultsjson-schema)
   - [Migration Report Execution Summary Table](#migration-report-execution-summary-table)
7. [CLI Reference & Unit Testing](#7-cli-reference--unit-testing)

---

## 1. Overview & Purpose

AUGUR relies heavily on high-throughput LLM inference across two compute-intensive phases:
1. **Synthetic Data Generation (SDG)**: Extracting structured AST/code and configuration metadata across every file in a repository (controlled by `SDG_LLM_MAX_CONCURRENCY`).
2. **GraphRAG Indexing**: Extracting entity-relationship graphs and community summaries across text chunks (controlled by `GRAPHRAG_CONCURRENT_REQUESTS`).

Hardcoding concurrency values across diverse hardware profiles (e.g., 1x NVIDIA L40S vs. 1x H100/H200 or multi-replica vLLM deployments) leads to either **under-utilization** (slow pipeline runs when concurrency is too low) or **request queuing, timeouts, and HTTP 429/5xx errors** (when concurrency saturates the KV cache).

The **GuideLLM Integration** ([`llm_benchmark.py`](workflows/examples/code_understanding/utils/llm_benchmark.py)) solves this by dynamically sweeping concurrency levels against the target vLLM / OpenAI-compatible inference endpoint and automatically configuring `SDG_LLM_MAX_CONCURRENCY` and `GRAPHRAG_CONCURRENT_REQUESTS` prior to pipeline execution.

```
       +--------------------------------------------------------------------------+
       |                    GuideLLM Concurrency Benchmarking                     |
       +--------------------------------------------------------------------------+
                     |                                              |
      [Mode 1: Standalone Job]                       [Mode 2: Pre-Pipeline KFP Step]
      `make benchmark-llm`                           `RUN_LLM_BENCHMARK=true`
                     |                                              |
                     v                                              v
       +-----------------------------------+          +-----------------------------------+
       | Kubernetes Job (`benchmark-llm`)  |          | KFP `benchmark_llm_op`            |
       | - Sweeps concurrency levels       |          | - Sweeps concurrency levels       |
       | - Streams ASCII table to console  |          | - Tracks duration in durations.json|
       | - Prints recommended .env values  |          | - Writes benchmark_results.json   |
       +-----------------------------------+          +-----------------------------------+
                                                                    |
                                         +--------------------------+--------------------------+
                                         | (Propagates optimal_concurrency)                    |
                                         v                                                     v
                         +-------------------------------+                     +-------------------------------+
                         | 1. DATA GENERATION (SDG Hub)  |                     | 2. GRAPHRAG INDEXING          |
                         | SDG_LLM_MAX_CONCURRENCY = N   |                     | GRAPHRAG_CONCURRENT_REQUESTS=N|
                         +-------------------------------+                     +-------------------------------+
                                         |                                                     |
                                         +--------------------------+--------------------------+
                                                                    |
                                                                    v
                                                  +-----------------------------------+
                                                  | 3. ANALYSIS & MIGRATION REPORT    |
                                                  | Includes "GuideLLM Concurrency    |
                                                  | Benchmark" in Execution Summary   |
                                                  +-----------------------------------+
```

---

## 2. Execution Modes

AUGUR supports two execution modes for GuideLLM concurrency benchmarking:

### Mode 1: Standalone OpenShift Job (`make benchmark-llm`)

Runs an on-demand concurrency sweep inside your OpenShift namespace via a Helm-managed Kubernetes `Job` ([`benchmark-llm-job.yaml`](resources/helm/templates/benchmark-llm-job.yaml)) and streams the latency/throughput report and recommended `.env` settings directly to your terminal.

#### Run in OpenShift Cluster (Default)
```bash
make benchmark-llm
```
What happens under the hood:
1. Loads configuration from `.env`.
2. Creates a fresh `benchmark-llm-scripts` `ConfigMap` in `$KFP_NAMESPACE` containing [`benchmark_llm.sh`](workflows/examples/code_understanding/scripts/benchmark_llm.sh) and [`llm_benchmark.py`](workflows/examples/code_understanding/utils/llm_benchmark.py).
3. Renders [`resources/helm/templates/benchmark-llm-job.yaml`](resources/helm/templates/benchmark-llm-job.yaml) (escaping comma-separated `--set-string` parameters) and submits `job.batch/benchmark-llm` using the `pipeline-tools` image.
4. Copies the mounted scripts into `/tmp` inside the pod (ensuring compatibility with OpenShift's arbitrary non-root UID security context) and executes the sweep against `$GRAPHRAG_LLM_API_BASE`.
5. Streams the real-time logs and final ASCII recommendation table to stdout, then cleans up the temporary `ConfigMap`.

#### Pass Custom Sweep Arguments
```bash
make benchmark-llm ARGS="--concurrency-levels 1,2,4,8,16,24,32 --max-seconds 20 --max-requests 20"
```

#### Run Locally Outside OpenShift
```bash
make benchmark-llm LOCAL=1
```

---

### Mode 2: Pre-Pipeline KFP Step (`benchmark_llm_op`)

Runs automatically as the first step of the Data Generation pipeline when `RUN_LLM_BENCHMARK=true` (the default in [`.env.template`](.env.template)).

- **Kubeflow Pipelines ([`pipelines/kubeflow/data_generation.py`](workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py))**:
  - Executes [`benchmark_llm_op`](workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py) before repository cloning and SDG metadata generation.
  - Passes the resulting `optimal_concurrency` integer directly into `generate_code_and_meta_op`.
- **Base Pipeline & Local Orchestrator ([`pipelines/base/data_generation.py`](workflows/examples/code_understanding/pipelines/base/data_generation.py))**:
  - Calls [`benchmark_and_configure_concurrency()`](workflows/examples/code_understanding/pipelines/base/data_generation.py) at the start of `DataGenerationPipeline.run()` and `DataGenerationPipeline.run_multi_repo()` if not already benchmarked in the parent KFP graph.
  - Records the wall-clock duration of the `"GuideLLM Concurrency Benchmark"` step via [`DurationTracker`](workflows/examples/code_understanding/utils/duration_tracker.py).
  - Persists `benchmark_results.json` and `durations.json` to the shared workspace/PVC so downstream Indexing and Analysis pods inherit both the optimal concurrency and the pre-pipeline step timing.

To bypass pre-pipeline benchmarking and use static concurrency values instead, set in `.env`:
```dotenv
RUN_LLM_BENCHMARK=false
SDG_LLM_MAX_CONCURRENCY=10
GRAPHRAG_CONCURRENT_REQUESTS=25
```

---

## 3. Core Architecture & Selection Algorithm

### Endpoint Resolution
[`resolve_llm_endpoint()`](workflows/examples/code_understanding/utils/llm_benchmark.py) resolves the target LLM endpoint, model identifier, and bearer token using the following priority order:

| Parameter | Priority 1 (CLI Arg) | Priority 2 (GuideLLM Env) | Priority 3 (GraphRAG Env) | Priority 4 (Code LLM Env) |
| :--- | :--- | :--- | :--- | :--- |
| **API Base URL** | `--api-base` | `GUIDELLM_TARGET` | `GRAPHRAG_LLM_API_BASE` | `CODE_LLM_API_BASE` |
| **Model ID** | `--model` | `GUIDELLM_MODEL` | `GRAPHRAG_LLM_ID` | `CODE_LLM_ID` |
| **API Key / Token** | `--api-key` | `GUIDELLM_API_KEY` | `GRAPHRAG_LLM_TOKEN` | `CODE_LLM_TOKEN` |

### Dual-Runner Architecture (GuideLLM CLI + Async HTTP Fallback)
[`run_guidellm_benchmark()`](workflows/examples/code_understanding/utils/llm_benchmark.py) uses a resilient two-tier execution strategy:
1. **Primary Runner (`guidellm` CLI)**:
   - Automatically detects the installed `guidellm` CLI syntax (`guidellm benchmark`, `guidellm benchmark run`, or `guidellm run`).
   - Configures writable `HOME` and `HF_HOME` paths under `/tmp` when running as an arbitrary OpenShift UID.
   - Respects `ENABLE_TLS_VERIFICATION` for self-signed cluster certificates.
   - Parses GuideLLM's JSON output (`parse_guidellm_json`) across multiple GuideLLM schema versions.
2. **Automatic Fallback Runner (`async_http_probe`)**:
   - If the `guidellm` CLI binary is unavailable or exits without producing metrics (e.g., due to offline HuggingFace tokenizer lookups in disconnected environments), `llm_benchmark.py` seamlessly falls back to an internal `asyncio` concurrent HTTP probe (`_run_async_probe_level`) against `/v1/chat/completions`.
   - Measures real wall-clock throughput (`output_tokens_per_second`, `requests_per_second`), mean request latency (`latency_mean_s`), and HTTP error rate at each concurrency level.

### Optimal Concurrency Selection (`select_best_concurrency`)
Given metrics across all tested concurrency levels $C = \{c_1, c_2, \dots, c_k\}$, [`select_best_concurrency()`](workflows/examples/code_understanding/utils/llm_benchmark.py) selects the optimal level using a **Knee-of-Curve / Near-Peak Throughput** strategy:

1. **Valid Progress Filter**: Keep only levels with `successful_requests > 0` and positive throughput (`output_tokens_per_second > 0` or `requests_per_second > 0`). If none succeed, return `default_concurrency` (fallback `10`).
2. **Error-Rate SLA Filter**: Keep levels with `error_rate <= max_error_rate` (default `10%`). If all levels exceed `max_error_rate`, fall back to the level(s) with the minimum observed error rate.
3. **Effective Throughput Scoring**: Compute the error-penalized throughput score for each candidate level:
   $$\text{Score}(c) = \text{Throughput}(c) \times \max(0,\, 1 - \text{ErrorRate}(c))$$
   where $\text{Throughput}(c)$ uses `output_tokens_per_second` when available, or `requests_per_second` otherwise.
4. **95% Peak Threshold (Plateau Avoidance)**: Identify $\text{Score}_{\max} = \max_c \text{Score}(c)$ and select the **smallest concurrency level** $c^*$ that achieves at least **95%** of peak effective throughput:
   $$c^* = \min \left\{ c \;\middle|\; \text{Score}(c) \ge 0.95 \times \text{Score}_{\max} \right\}$$
   This prevents over-saturating the LLM server with 32+ concurrent streams when 8 or 16 streams already achieve peak token generation throughput at lower latency.

---

## 4. End-to-End Pipeline Propagation & Safeguards

### Pre-Pipeline Benchmark Step
- In [`benchmark_and_configure_concurrency()`](workflows/examples/code_understanding/pipelines/base/data_generation.py):
  - Wraps the sweep in `DurationTracker.measure(stage="Data Generation", step="GuideLLM Concurrency Benchmark")`.
  - Calls [`apply_best_concurrency(best_concurrency)`](workflows/examples/code_understanding/utils/llm_benchmark.py) to set `SDG_LLM_MAX_CONCURRENCY` and `GRAPHRAG_CONCURRENT_REQUESTS` in `os.environ`.
  - Saves `benchmark_results.json` and `durations.json` to candidate workspace directories (`target`, `source`, and repository slug subdirectories) and uploads durations to MLflow.

### Stage 1: Synthetic Data Generation (`SDG_LLM_MAX_CONCURRENCY`)
- In [`generate_code_and_meta()`](workflows/examples/code_understanding/pipelines/base/data_generation.py):
  - Reads `max_concurrency = int(os.getenv("SDG_LLM_MAX_CONCURRENCY", "10"))`.
  - Passes `max_concurrency` directly into SDG Hub's `flow.generate(dataset, max_concurrency=max_concurrency)`.

### Stage 2: GraphRAG Indexing (`GRAPHRAG_CONCURRENT_REQUESTS`)
- In [`generate_graphrag_index()`](workflows/examples/code_understanding/pipelines/base/indexing.py):
  - Discovers `benchmark_results.json` from upstream directories via `find_all_telemetry_files(candidate_dirs, "benchmark_results.json")` and applies `best_concurrency` to `SDG_LLM_MAX_CONCURRENCY` and `GRAPHRAG_CONCURRENT_REQUESTS`.
  - Also extracts the benchmark step duration via `extract_benchmark_duration(candidate_dirs, dur_tracker)` if `durations.json` was not yet merged.
- In [`DependencyAnalyzer.prepare_settings()`](workflows/examples/code_understanding/utils/graphrag_utils.py) and [`settings.yaml.in`](workflows/examples/code_understanding/assets/graphrag/settings.yaml.in):
  - Uses `concurrent_requests: ${GRAPHRAG_CONCURRENT_REQUESTS}` and `max_tokens: 8192` under `models.default_chat_model`.
  - Includes a runtime safeguard in `prepare_settings()` that automatically patches stale MLflow-downloaded `settings.yaml.in` templates if they still contain a hardcoded `concurrent_requests: 25` or lack `max_tokens`.

### Stage 3: Analysis & Migration Report Duration Table
- [`DurationTracker.CANONICAL_STEPS`](workflows/examples/code_understanding/utils/duration_tracker.py) and [`analysis.py`](workflows/examples/code_understanding/pipelines/base/analysis.py) register `"GuideLLM Concurrency Benchmark"` as the first canonical step under `"Data Generation"`.
- Both the console ASCII duration table and the Markdown `### Pipeline Execution Summary` table in `migration_report.md` display `"GuideLLM Concurrency Benchmark"` with its measured duration (or `Skipped / N/A` when `RUN_LLM_BENCHMARK=false`).

### Telemetry Artifact Exclusion Safeguards
To ensure `benchmark_results.json` and other telemetry files (`durations.json`, `tokens.json`, `project_metrics.json`) never pollute the analyzed repository dataset:
- [`CodeMetricsTracker`](workflows/examples/code_understanding/utils/code_metrics_tracker.py) excludes `benchmark_results.json` from repository file and SLOC counts.
- [`data_generation.py`](workflows/examples/code_understanding/pipelines/base/data_generation.py) excludes telemetry `.json`, `.txt`, and `_metadata.txt` files when constructing `sdg_dataset.parquet`.
- [`indexing.py`](workflows/examples/code_understanding/pipelines/base/indexing.py) strips telemetry `.json`, `.txt`, and `_metadata.txt` files from `graphrag_source_path/input` before running GraphRAG indexing.

---

## 5. Configuration & Environment Variables

All GuideLLM settings are configured at the bottom of [`.env.template`](.env.template) and `.env`:

| Variable | Default | Description |
| :--- | :---: | :--- |
| `RUN_LLM_BENCHMARK` | `true` | Enables (`true`) or bypasses (`false`) the pre-pipeline GuideLLM concurrency benchmark step. |
| `GUIDELLM_CONCURRENCY_LEVELS` | `1,2,4,8,16,32` | Comma-separated list of concurrency levels to sweep during benchmarking. |
| `GUIDELLM_MAX_SECONDS` | `15` | Maximum duration (in seconds) per concurrency level during the sweep. |
| `GUIDELLM_MAX_REQUESTS` | `16` | Maximum number of requests sent per concurrency level during the sweep. |
| `GUIDELLM_PROMPT_TOKENS` | `512` | Synthetic prompt token length used by the GuideLLM CLI runner. |
| `GUIDELLM_OUTPUT_TOKENS` | `256` | Synthetic output completion token length used by the benchmark runner. |
| `GUIDELLM_TARGET` | *(fallback)* | Optional override for target API base URL (falls back to `GRAPHRAG_LLM_API_BASE` / `CODE_LLM_API_BASE`). |
| `GUIDELLM_MODEL` | *(fallback)* | Optional override for target model ID (falls back to `GRAPHRAG_LLM_ID` / `CODE_LLM_ID`). |
| `GUIDELLM_API_KEY` | *(fallback)* | Optional override for target API token (falls back to `GRAPHRAG_LLM_TOKEN` / `CODE_LLM_TOKEN`). |
| `SDG_LLM_MAX_CONCURRENCY` | `10` | Max concurrent LLM requests during Data Generation (auto-tuned when benchmark runs). |
| `GRAPHRAG_CONCURRENT_REQUESTS` | `25` | Max concurrent LLM requests during GraphRAG Indexing (auto-tuned when benchmark runs). |

---

## 6. Artifact Formats & Console Outputs

### Console ASCII Benchmark Summary
Both `make benchmark-llm` and the pre-pipeline `benchmark_llm_op` step print a formatted summary table via [`format_benchmark_summary()`](workflows/examples/code_understanding/utils/llm_benchmark.py) (with `*` marking the selected optimal concurrency):

```text
======================================================================================
GuideLLM Concurrency Benchmark Summary
======================================================================================
  Target Endpoint : http://vllm-endpoint:8000/v1
  Model           : openai/gpt-oss-120b
  Runner          : guidellm
  Total Duration  : 48.32s
--------------------------------------------------------------------------------------
  Concurrency |   Out Tok/s |     Req/s | Mean Lat (s) | Success | Errors |  Err %
--------------------------------------------------------------------------------------
           1 |       42.10 |      0.16 |        6.080 |       3 |      0 |   0.0%
           2 |       81.45 |      0.32 |        6.290 |       5 |      0 |   0.0%
           4 |      154.20 |      0.60 |        6.640 |       9 |      0 |   0.0%
           8 |      278.90 |      1.09 |        7.340 |      16 |      0 |   0.0%
 *        16 |      342.60 |      1.34 |       11.950 |      16 |      0 |   0.0%
          32 |      348.10 |      1.36 |       23.510 |      16 |      0 |   0.0%
--------------------------------------------------------------------------------------
  Recommended Concurrency : 16
    SDG_LLM_MAX_CONCURRENCY=16
    GRAPHRAG_CONCURRENT_REQUESTS=16
======================================================================================
```

### `benchmark_results.json` Schema
Saved to the pipeline workspace directory by [`save_benchmark_result()`](workflows/examples/code_understanding/utils/llm_benchmark.py):

```json
{
  "best_concurrency": 16,
  "api_base": "http://vllm-endpoint:8000/v1",
  "model": "openai/gpt-oss-120b",
  "runner": "guidellm",
  "skipped": false,
  "duration_seconds": 48.32,
  "levels": [
    {
      "concurrency": 1,
      "requests_per_second": 0.16,
      "output_tokens_per_second": 42.1,
      "tokens_per_second": 126.3,
      "latency_mean_s": 6.08,
      "ttft_ms": 112.4,
      "itl_ms": 23.1,
      "successful_requests": 3,
      "failed_requests": 0,
      "error_rate": 0.0
    }
  ]
}
```

### Migration Report Execution Summary Table
In the final `migration_report.md`, the pre-pipeline step appears at the top of the **Data Generation** stage in the **Pipeline Execution Summary** table:

| Pipeline Stage | Step | Duration | % of Total |
| :--- | :--- | :---: | :---: |
| **Data Generation** | GuideLLM Concurrency Benchmark | 48.32s | 3.4% |
| **Data Generation** | Clone Repository | 2.14s | 0.2% |
| **Data Generation** | Detect Languages & Measure Code Metrics | 0.85s | 0.1% |

---

## 7. CLI Reference & Unit Testing

### Direct CLI Usage
You can invoke [`llm_benchmark.py`](workflows/examples/code_understanding/utils/llm_benchmark.py) or [`benchmark_llm.sh`](workflows/examples/code_understanding/scripts/benchmark_llm.sh) directly:

```bash
python workflows/examples/code_understanding/utils/llm_benchmark.py \
  --api-base "http://vllm-endpoint:8000/v1" \
  --model "openai/gpt-oss-120b" \
  --concurrency-levels "1,2,4,8,16,32" \
  --max-seconds 15 \
  --max-requests 16 \
  --output-file benchmark_results.json
```

### Running the Unit Test Suite
[`test_llm_benchmark.py`](workflows/examples/code_understanding/tests/test_llm_benchmark.py) validates concurrency level parsing, endpoint resolution, GuideLLM JSON parsing across schema versions, async HTTP fallback probing, knee-of-curve concurrency selection, environment variable application, and `DurationTracker` integration:

```bash
python -m unittest workflows/examples/code_understanding/tests/test_llm_benchmark.py
```
