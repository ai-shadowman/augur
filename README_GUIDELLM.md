# AUGUR GuideLLM Telemetry & Auto-Tuning: BenchmarkTracker Architecture & Reference Guide
*Comprehensive guide for LLM inference benchmarking, SLO verification, automatic concurrency calibration, and runtime estimation on Red Hat OpenShift AI.*

---

## Table of Contents
1. [Overview & Purpose](#1-overview--purpose)
2. [Core Architecture & Execution Modes](#2-core-architecture--execution-modes)
   - [Fast Pre-Check Probe (`pre_check_pipelines.sh`)](#fast-pre-check-probe-pre_check_pipelinessh)
   - [Stage 0 Automatic Pipeline Calibration (`guidellm_calibration_op`)](#stage-0-automatic-pipeline-calibration-guidellm_calibration_op)
   - [Standalone OpenShift Benchmark Job (`make benchmark-llm`)](#standalone-openshift-benchmark-job-make-benchmark-llm)
   - [Built-In Async Streaming Fallback](#built-in-async-streaming-fallback)
3. [AUGUR Workload Profiles & Concurrency Selection](#3-augur-workload-profiles--concurrency-selection)
   - [Workload Token Profiles](#workload-token-profiles)
   - [Optimal Concurrency & Rate-Limit Calculation](#optimal-concurrency--rate-limit-calculation)
   - [Pipeline Runtime Estimator](#pipeline-runtime-estimator)
4. [Disconnected / Air-Gapped OpenShift Packaging](#4-disconnected--air-gapped-openshift-packaging)
5. [Class API Reference (`BenchmarkTracker`)](#5-class-api-reference-benchmarktracker)
   - [Lifecycle & Initialization](#lifecycle--initialization)
   - [Benchmark Execution & Parsing](#benchmark-execution--parsing)
   - [Auto-Tuning & Runtime Estimation](#auto-tuning--runtime-estimation)
   - [Serialization, MLflow & S3 Persistence](#serialization-mlflow--s3-persistence)
   - [Formatting & Report Injection](#formatting--report-injection)
6. [End-to-End Pipeline Integration Walkthrough](#6-end-to-end-pipeline-integration-walkthrough)
7. [Artifact Formats & Presentation Outputs](#7-artifact-formats--presentation-outputs)
   - [`benchmarks.json` Schema](#benchmarksjson-schema)
   - [Console ASCII Summary Table](#console-ascii-summary-table)
   - [Markdown Report Section (`migration_report.md`)](#markdown-report-section-migration_reportmd)
   - [MLflow Scalar Metrics](#mlflow-scalar-metrics)
8. [Configuration & Quickstart Commands](#8-configuration--quickstart-commands)

---

## 1. Overview & Purpose

In enterprise code modernization workflows on **Red Hat OpenShift AI (RHOAI)**, LLM serving performance directly governs pipeline stability and execution time. Static concurrency settings (`SDG_LLM_MAX_CONCURRENCY=10`, GraphRAG `concurrent_requests: 25`) either:
- **Overwhelm KServe / vLLM KV-caches**, causing HTTP 429s, 600-second request timeouts, and long retry backoffs during GraphRAG indexing; or
- **Underutilize high-memory GPUs** (`NVIDIA H100`, `H200`, `L40S`), leaving substantial throughput on the table.

Furthermore, wall-clock duration metrics (`DurationTracker`) combine network latency, client retry sleeps, queuing delays, and token generation into a single number without exposing underlying server health.

AUGUR integrates **[GuideLLM](https://github.com/vllm-project/guidellm)** and [`BenchmarkTracker`](workflows/examples/code_understanding/utils/benchmark_tracker.py) across all pipeline stages to provide:
1. **Granular Inference Server Telemetry**: Tracks **Time to First Token (TTFT)** (`p50`, `p95`, `p99` in ms), **Inter-Token Latency (ITL)** (`p50`, `p95`, `p99` in ms), **Requests/sec (RPS)**, and **Sustained Output Throughput (tok/s)**.
2. **Pre-Flight SLO Gate**: Validates that the target LLM endpoint can sustain concurrent streaming generation above a minimum token/sec threshold before submitting Kubeflow Pipeline runs.
3. **Automatic Concurrency & Rate-Limit Calibration**: Dynamically tunes Stage 1 (`SDG_LLM_MAX_CONCURRENCY`) and Stage 2 (`concurrent_requests`, `requests_per_minute`, `tokens_per_minute` in [`settings.yaml.in`](workflows/examples/code_understanding/assets/graphrag/settings.yaml.in)) based on measured endpoint capacity.
4. **Upfront Pipeline Runtime Estimation**: Combines [`CodeMetricsTracker`](workflows/examples/code_understanding/utils/code_metrics_tracker.py) file/SLOC counts with GuideLLM sustained `output_tokens_per_second` to log an estimated completion time for Stages 1, 2, and 3.

```
        +--------------------------------------------------------------------------+
        |                    AUGUR + GuideLLM Pipeline Workflow                    |
        +--------------------------------------------------------------------------+
                                           |
             0. PRE-CHECK & STAGE 0        v
        +--------------------------------------------------------------------------+
        | - Fast GuideLLM Probe in pre_check_pipelines.sh / run_pipelines.sh       |
        | - KFP Stage 0: guidellm_calibration_op sweeps concurrency levels         |
        | - Saves benchmarks.json & uploads telemetry/metrics to MLflow            |
        +--------------------------------------------------------------------------+
                                           | (KFP DAG / MLflow Sync)
             1. DATA GENERATION            v
        +--------------------------------------------------------------------------+
        | - Downloads benchmarks.json from MLflow                                  |
        | - Auto-tunes SDG_LLM_MAX_CONCURRENCY (scaled by GRAPHRAG_PARALLEL_REPOS) |
        | - Combines CodeMetricsTracker file count + tok/s -> Runtime Estimate     |
        +--------------------------------------------------------------------------+
                                           | (KFP Dataset Artifact / MLflow Sync)
             2. GRAPHRAG INDEXING          v
        +--------------------------------------------------------------------------+
        | - DependencyAnalyzer.prepare_settings() reads BenchmarkTracker limits    |
        | - Auto-populates concurrent_requests, requests_per_minute, and TPM       |
        | - Propagates benchmarks.json to GraphRAG output directory & MLflow       |
        +--------------------------------------------------------------------------+
                                           | (KFP Dataset Artifact / MLflow Sync)
             3. ANALYSIS & REPORT          v
        +--------------------------------------------------------------------------+
        | - Merges benchmarks.json from upstream artifacts / MLflow                |
        | - Appends "### LLM Inference Server Benchmark & Calibration (GuideLLM)"  |
        |   table into migration_report_<git_slug>.md                              |
        +--------------------------------------------------------------------------+
```

---

## 2. Core Architecture & Execution Modes

### Fast Pre-Check Probe (`pre_check_pipelines.sh`)
Before triggering a Kubeflow Pipeline run, [`pre_check_pipelines.sh`](workflows/examples/code_understanding/scripts/pre_check_pipelines.sh) (invoked automatically by [`run_pipelines.sh`](workflows/examples/code_understanding/scripts/run_pipelines.sh) when `GUIDELLM_PRECHECK_ENABLED=true`) runs a fast 15-second concurrent probe (`workload=precheck`, `rates=1,4`) against `GRAPHRAG_LLM_API_BASE`:
- Verifies that the inference server completes concurrent streaming generations with `0%` fatal error rate and meets `GUIDELLM_MIN_TOK_PER_SEC` (default: `5.0 tok/s`).
- Exports auto-tuned `SDG_LLM_MAX_CONCURRENCY`, `GRAPHRAG_CONCURRENT_REQUESTS`, `GRAPHRAG_REQUESTS_PER_MINUTE`, and `GRAPHRAG_TOKENS_PER_MINUTE` into the environment.

### Stage 0 Automatic Pipeline Calibration (`guidellm_calibration_op`)
In [`orchestrator.py`](workflows/examples/code_understanding/pipelines/orchestrator.py), both [`single_repo_pipeline`](workflows/examples/code_understanding/pipelines/orchestrator.py#L35-L100) and [`multi_repo_pipeline`](workflows/examples/code_understanding/pipelines/orchestrator.py#L103-L133) wire [`DataGenerationPipeline.calibrate`](workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py#L26-L78) (`guidellm_calibration_op`) ahead of Stage 1:
- Runs inside the `pipeline-tools` container image using credentials from `secret/code-understanding-env`.
- Sweeps configured concurrency levels (`GUIDELLM_RATES`, default `1,4,8,16`), computes optimal concurrency and rate limits, and uploads `telemetry/benchmarks.json` to the active MLflow run so all downstream single-repo or parallel multi-repo pods inherit the calibration automatically.

### Standalone OpenShift Benchmark Job (`make benchmark-llm`)
For dedicated model evaluation, hardware sizing (`L40S` vs. `H100` vs. `H200`), or vLLM flag tuning without launching a full codebase pipeline:
- Managed via [`resources/helm/templates/run-guidellm-job.yaml`](resources/helm/templates/run-guidellm-job.yaml) and [`workflows/examples/code_understanding/scripts/run_guidellm.sh`](workflows/examples/code_understanding/scripts/run_guidellm.sh).
- Mounts OpenShift's `odh-trusted-ca-bundle` ConfigMap via the `concatenate-all-ca-trusts` `initContainer` so internal KServe TLS routes are trusted automatically.

### Built-In Async Streaming Fallback
If the `guidellm` CLI binary is not installed in a local developer virtual environment, [`BenchmarkTracker._run_builtin_async_probe()`](workflows/examples/code_understanding/utils/benchmark_tracker.py#L529-L664) automatically falls back to a native `AsyncOpenAI` streaming benchmark probe that measures exact per-stream TTFT (`p50`/`p95`/`p99`), ITL (`p50`/`p95`/`p99`), RPS, and output token throughput using the same synthetic token profiles.

---

## 3. AUGUR Workload Profiles & Concurrency Selection

### Workload Token Profiles
Defined in `AUGUR_WORKLOAD_PROFILES` inside [`benchmark_tracker.py`](workflows/examples/code_understanding/utils/benchmark_tracker.py#L46-L77):

| Profile Name | AUGUR Pipeline Stage | Prompt Tokens | Output Tokens | Default Concurrency Sweep | Default Max Seconds |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **`precheck`** | Pre-flight readiness gate | `256` | `64` | `1,4` | `15s` |
| **`sdg`** | Stage 1: `sdg_hub` AST/file JSON metadata extraction | `2,048` | `450` | `1,5,10,15,20` | `30s` |
| **`indexing`** | Stage 2: GraphRAG entity/relationship & community extraction | `1,500` | `600` | `1,5,10,20,25` | `30s` |
| **`analysis`** | Stage 3: Architectural & migration report synthesis | `6,000` | `1,500` | `1,2,4,8` | `45s` |

### Optimal Concurrency & Rate-Limit Calculation
[`BenchmarkTracker._recompute_recommendations()`](workflows/examples/code_understanding/utils/benchmark_tracker.py#L266-L336) evaluates all recorded concurrency sweep points for each LLM role (`graphrag`, `ground_truth`, `judge`):
1. **SLO Filtering**: Selects sweep points where `error_rate <= GUIDELLM_MAX_ERROR_RATE` (default `5%`), `ttft_ms_p95 <= max(GUIDELLM_MAX_TTFT_P95_MS, baseline_ttft_p95 * 2.5)` (default `12,000 ms`), and `itl_ms_p95 <= GUIDELLM_MAX_ITL_P95_MS` (default `250 ms`).
2. **Inflection Point Selection**: Picks the lowest concurrency level that achieves $\ge 90\%$ of peak sustained `output_tokens_per_second`, avoiding KV-cache saturation and tail-latency spikes.
3. **Derived Pipeline Parameters** (with a $90\%$ safety headroom factor and multi-repo parallelism awareness):
   - `recommended_graphrag_concurrency` = `optimal_concurrency`
   - `recommended_sdg_concurrency` = `max(1, optimal_concurrency // GRAPHRAG_PARALLEL_REPOS)`
   - `recommended_rpm` = $\lfloor \text{RPS} \times 60 \times 0.90 \rfloor$
   - `recommended_tpm` = $\lfloor \text{Total tok/s} \times 60 \times 0.90 \rfloor$

### Pipeline Runtime Estimator
[`BenchmarkTracker.estimate_pipeline_runtime(total_files, total_lines)`](workflows/examples/code_understanding/utils/benchmark_tracker.py#L389-L426) combines repository file counts from [`CodeMetricsTracker`](workflows/examples/code_understanding/utils/code_metrics_tracker.py) with sustained `output_tokens_per_second`:
- **Stage 1 (`Data Generation`)**: $\approx 400 \text{ output tokens/file}$
- **Stage 2 (`GraphRAG Indexing`)**: $\approx 750 \text{ output tokens/file}$
- **Stage 3 (`Migration Analysis`)**: $\approx 18,000 \text{ output tokens (12 prompts)}$
- Applies a $1.15\times$ non-LLM I/O and embedding overhead factor and logs the per-stage and total ETA to pod stdout at the start of Stage 1.

---

## 4. Disconnected / Air-Gapped OpenShift Packaging

GuideLLM requires a HuggingFace tokenizer (`--processor`) to synthesize prompt token lengths and count generated completion tokens. In disconnected OpenShift clusters without internet access to `huggingface.co`, runtime tokenizer downloads fail.

To guarantee offline execution:
- [`resources/images/pipeline-tools/requirements.txt`](resources/images/pipeline-tools/requirements.txt) installs `guidellm>=0.2.1`.
- [`resources/images/pipeline-tools/Containerfile`](resources/images/pipeline-tools/Containerfile) pre-downloads the `gpt2` tokenizer at image build time into `/opt/app-root/src/.cache/huggingface/gpt2` and sets:
  ```dockerfile
  ENV HF_HOME=/opt/app-root/src/.cache/huggingface \
      GUIDELLM_DEFAULT_PROCESSOR=/opt/app-root/src/.cache/huggingface/gpt2
  ```
- At runtime, [`resolve_default_processor()`](workflows/examples/code_understanding/utils/benchmark_tracker.py#L140-L157) automatically resolves the local pre-cached tokenizer path whenever present.

---

## 5. Class API Reference (`BenchmarkTracker`)

### Lifecycle & Initialization
- `BenchmarkTracker.get_instance(git_slug=None, git_repo=None, run_id=None) -> BenchmarkTracker`: Returns the process-wide singleton and backfills any missing metadata.
- `BenchmarkTracker.reset_instance(git_slug=None, git_repo=None, run_id=None) -> BenchmarkTracker`: Resets the singleton for a clean run or unit test.

### Benchmark Execution & Parsing
- `tracker.run_benchmark(role="graphrag", workload="indexing", profile=None, rates_str=None, max_seconds=None, output_dir=None) -> int`: Runs the `guidellm benchmark` CLI (or the built-in async streaming probe fallback) and records all sweep points.
- `tracker.run_guidellm_profile(profile_name="sdg", rates=None, max_seconds=15, role="graphrag") -> int`: Convenience wrapper used by pipeline stages.
- `tracker.parse_guidellm_json(filepath, role="graphrag", workload="indexing", model_id=None, api_base=None) -> int`: Parses GuideLLM JSON output (`v0.2+` and `v0.3+` schemas) into normalized telemetry records.

### Auto-Tuning & Runtime Estimation
- `tracker.get_recommended_sdg_concurrency(default=10, role="graphrag") -> int`: Returns the calibrated `SDG_LLM_MAX_CONCURRENCY`.
- `tracker.get_recommended_graphrag_limits(role="graphrag", default_concurrency=25) -> Dict[str, Any]`: Returns `concurrent_requests`, `requests_per_minute`, `tokens_per_minute`, `sdg_max_concurrency`, and `sustained_output_tokens_per_second`.
- `tracker.estimate_pipeline_runtime(total_files, total_lines=0) -> Optional[Dict[str, Any]]`: Returns estimated seconds for Stage 1, Stage 2, Stage 3, and total minutes.

### Serialization, MLflow & S3 Persistence
- `tracker.save_to_file(filepath)` / `BenchmarkTracker.load_from_file(filepath)` / `tracker.load_and_merge(filepath)`: Serializes and merges `benchmarks.json` across pipeline directories.
- `tracker.log_to_mlflow(run_id=None)`: Logs scalar metrics (`guidellm_<role>_optimal_concurrency`, `guidellm_<role>_output_tokens_per_sec`, `guidellm_<role>_ttft_ms_p50`, etc.) to MLflow.
- `tracker.upload_to_mlflow(git_slug=None, stage=None, multi_repo=False, run_id=None)`: Uploads `telemetry/benchmarks.json` (and any `benchmarks.html` reports) to MLflow and `DefaultAssetLoader`.
- `tracker.download_from_mlflow(git_slug=None, multi_repo=False, current_stage=None, run_id=None)`: Downloads and merges `telemetry/benchmarks.json` from MLflow or S3.

### Formatting & Report Injection
- `tracker.format_summary() -> str`: Renders the ASCII summary table for container logs.
- `tracker.format_markdown_section()` / `tracker.generate_markdown_summary() -> str`: Renders the Markdown section injected into `migration_report_<git_slug>.md`.

---

## 6. End-to-End Pipeline Integration Walkthrough

1. **Pre-Check & Stage 0 (`run_pipelines.sh` & `kubeflow/data_generation.py`)**:
   - Running `make run-pipelines ARGS="--single-repo"` or `make run-pipelines ARGS="--multi-repo"` executes the fast pre-check probe and triggers `guidellm_calibration_op` in Kubeflow Pipelines.
   - `guidellm_calibration_op` sweeps concurrency levels (`GUIDELLM_RATES`), uploads `telemetry/benchmarks.json` to MLflow, and logs scalar metrics.
2. **Stage 1 (`pipelines/base/data_generation.py`)**:
   - Downloads `benchmarks.json` from MLflow (or runs an inline calibration if executing standalone locally).
   - Sets `SDG_LLM_MAX_CONCURRENCY` to `bt.get_recommended_sdg_concurrency()` when `GUIDELLM_AUTO_TUNE=true`.
   - Computes and logs the repository's **Estimated Pipeline Runtime** right after `CodeMetricsTracker.measure_repository()`.
   - Saves `benchmarks.json` into the Stage 1 target artifact directory.
3. **Stage 2 (`utils/graphrag_utils.py` & `pipelines/base/indexing.py`)**:
   - [`DependencyAnalyzer.prepare_settings()`](workflows/examples/code_understanding/utils/graphrag_utils.py#L528-L610) loads `BenchmarkTracker` recommendations and substitutes `GRAPHRAG_CONCURRENT_REQUESTS`, `GRAPHRAG_REQUESTS_PER_MINUTE`, and `GRAPHRAG_TOKENS_PER_MINUTE` into [`settings.yaml.in`](workflows/examples/code_understanding/assets/graphrag/settings.yaml.in).
   - Preserves `benchmarks.json` in the GraphRAG output artifact directory.
4. **Stage 3 (`pipelines/base/analysis.py`)**:
   - Loads `benchmarks.json` from the GraphRAG directory / MLflow, prints the final ASCII summary table to pod logs, and appends the `### LLM Inference Server Benchmark & Calibration (GuideLLM)` section to `migration_report_<git_slug>.md`.

---

## 7. Artifact Formats & Presentation Outputs

### `benchmarks.json` Schema
```json
{
  "records": [
    {
      "role": "graphrag",
      "workload": "indexing",
      "model_id": "Granite-3.3-8B-Instruct",
      "api_base": "https://vllm.test/v1",
      "concurrency": 10,
      "requests_per_second": 1.8,
      "output_tokens_per_second": 145.0,
      "total_tokens_per_second": 820.0,
      "ttft_ms_mean": 320.0,
      "ttft_ms_p50": 290.0,
      "ttft_ms_p95": 610.0,
      "ttft_ms_p99": 890.0,
      "itl_ms_mean": 18.0,
      "itl_ms_p50": 16.5,
      "itl_ms_p95": 28.0,
      "itl_ms_p99": 39.0,
      "request_latency_sec_mean": 4.2,
      "request_latency_sec_p95": 6.1,
      "completed_requests": 25,
      "errored_requests": 0,
      "error_rate": 0.0,
      "prompt_tokens": 1500,
      "output_tokens": 512,
      "source": "guidellm"
    }
  ],
  "recommendations": {
    "graphrag": {
      "role": "graphrag",
      "model_id": "Granite-3.3-8B-Instruct",
      "workload": "indexing",
      "optimal_concurrency": 10,
      "recommended_sdg_concurrency": 5,
      "recommended_graphrag_concurrency": 10,
      "recommended_rpm": 97,
      "recommended_tpm": 44280,
      "sustained_output_tokens_per_second": 145.0,
      "sustained_total_tokens_per_second": 820.0,
      "sustained_requests_per_second": 1.8,
      "ttft_ms_p50": 290.0,
      "ttft_ms_p95": 610.0,
      "ttft_ms_p99": 890.0,
      "itl_ms_p50": 16.5,
      "itl_ms_p95": 28.0,
      "itl_ms_p99": 39.0,
      "error_rate": 0.0
    }
  },
  "git_slug": "test-repo"
}
```

### Console ASCII Summary Table
```
                  LLM INFERENCE BENCHMARK & CONCURRENCY CALIBRATION (GUIDELLM)                  
================================================================================================
 Role         Workload   Conc  RPS     Out tok/s   TTFT p50/p95 (ms)   ITL p50/p95 (ms)  Err %  
------------------------------------------------------------------------------------------------
 graphrag     indexing   10    1.80    145.0       290 / 610           16.5 / 28.0       0.0%   
------------------------------------------------------------------------------------------------
 [RECOMMENDED GRAPHRAG] Model: Granite-3.3-8B-Instruct | Optimal Concurrency: 10 (SDG: 5) | RPM: 97 | TPM: 44,280
================================================================================================
```

### Markdown Report Section (`migration_report.md`)
Injected automatically at the bottom of `migration_report_<git_slug>.md` alongside `CodeMetricsTracker`, `TokenCostTracker`, and `DurationTracker`:
````markdown
### LLM Inference Server Benchmark & Calibration (GuideLLM)

Measured inference server latency (**TTFT** / **ITL**), token throughput, and auto-tuned concurrency limits:

```
                  LLM INFERENCE BENCHMARK & CONCURRENCY CALIBRATION (GUIDELLM)                  
================================================================================================
 Role         Workload   Conc  RPS     Out tok/s   TTFT p50/p95 (ms)   ITL p50/p95 (ms)  Err %  
------------------------------------------------------------------------------------------------
 graphrag     indexing   10    1.80    145.0       290 / 610           16.5 / 28.0       0.0%   
------------------------------------------------------------------------------------------------
 [RECOMMENDED GRAPHRAG] Model: Granite-3.3-8B-Instruct | Optimal Concurrency: 10 (SDG: 5) | RPM: 97 | TPM: 44,280
================================================================================================
```
````

### MLflow Scalar Metrics
Logged under the active MLflow run:
- `guidellm_<role>_optimal_concurrency`
- `guidellm_<role>_output_tokens_per_sec`
- `guidellm_<role>_requests_per_sec`
- `guidellm_<role>_ttft_ms_p50`, `guidellm_<role>_ttft_ms_p95`, `guidellm_<role>_ttft_ms_p99`
- `guidellm_<role>_itl_ms_p50`, `guidellm_<role>_itl_ms_p95`
- `guidellm_<role>_recommended_rpm`, `guidellm_<role>_recommended_tpm`

---

## 8. Configuration & Quickstart Commands

### Environment Variables (`.env`)
| Variable | Default | Description |
| :--- | :---: | :--- |
| `GUIDELLM_PRECHECK_ENABLED` | `true` | Run the fast 15s GuideLLM probe during `pre_check_pipelines.sh` / `run_pipelines.sh`. |
| `GUIDELLM_CALIBRATE_PIPELINE` | `true` | Run Stage 0 `guidellm_calibration_op` before Stage 1 (`Data Generation`). |
| `GUIDELLM_AUTO_TUNE` | `true` | Automatically apply calibrated concurrency, RPM, and TPM limits to Stage 1 & Stage 2. |
| `GUIDELLM_TARGET_ROLE` | `graphrag` | Target LLM endpoint role (`graphrag`, `ground_truth`, `judge`, or `all`). |
| `GUIDELLM_WORKLOAD` | `indexing` | Workload token profile (`precheck`, `sdg`, `indexing`, `analysis`, or `all`). |
| `GUIDELLM_PROFILE` | `concurrent` | GuideLLM rate strategy (`concurrent`, `sweep`, `throughput`, `synchronous`). |
| `GUIDELLM_RATES` | `1,4,8,16` | Comma-separated concurrency levels to sweep during pipeline calibration. |
| `GUIDELLM_MAX_SECONDS` | `20` | Duration (seconds) per concurrency rate step. |
| `GUIDELLM_MIN_TOK_PER_SEC` | `5.0` | Minimum sustained output tok/s required when `--enforce-slo` is enabled. |
| `GUIDELLM_DEFAULT_PROCESSOR` | `/opt/app-root/src/.cache/huggingface/gpt2` | Pre-baked local HuggingFace tokenizer directory for disconnected clusters. |

### Commands
1. **Build and push images (includes `pipeline-tools` with `guidellm` and offline tokenizer)**:
   ```bash
   make build-images
   ```
   *(Or build directly on OpenShift via BuildConfigs: `make oc-build-images`)*

2. **Upload compiled Kubeflow Pipelines (includes Stage 0 GuideLLM calibration)**:
   ```bash
   make upload-pipelines
   ```

3. **Run AUGUR pipelines with automatic GuideLLM pre-check + Stage 0 calibration**:
   ```bash
   # Single-repository pipeline
   make run-pipelines ARGS="--single-repo"

   # Multi-repository pipeline
   make run-pipelines ARGS="--multi-repo"
   ```
   *(To skip GuideLLM for a specific run, pass `ARGS="--single-repo --no-guidellm-check --no-benchmark-llm"`)*

4. **Run a standalone GuideLLM benchmark Job on OpenShift**:
   ```bash
   make benchmark-llm
   ```
   *(Or locally: `make benchmark-llm-local`)*
