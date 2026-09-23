# AUGUR Telemetry, Token Usage & Duration Tracking Architecture
*Maintenance & Extensibility Guide for the `token_metrics_telemetry` Updates*

---

## Table of Contents
1. [Overview & Purpose](#overview--purpose)
2. [What Are the Current (Existing) Items?](#what-are-the-current-existing-items)
3. [What Are the New Items?](#what-are-the-new-items)
   - [LLM Token Usage & Cost Tracking Subsystem](#1-llm-token-usage--cost-tracking-subsystem)
   - [Pipeline Step & Stage Duration Tracking Subsystem](#2-pipeline-step--stage-duration-tracking-subsystem)
   - [In-Process GraphRAG Execution (`graphrag.py`)](#3-in-process-graphrag-execution-graphragpy)
   - [Telemetry Abstraction Layer (`telemetry/`)](#4-telemetry-abstraction-layer-telemetry)
   - [Cross-Pod Telemetry Propagation (Kubeflow & Local)](#5-cross-pod-telemetry-propagation-kubeflow--local)
   - [OpenTelemetry & Tempo Tracing Infrastructure](#6-opentelemetry--tempo-tracing-infrastructure)
   - [Test Suites](#7-test-suites)
4. [File-by-File Changes Summary](#file-by-file-changes-summary)
5. [Step-by-Step Guide: How to Add an Additional Metric](#step-by-step-guide-how-to-add-an-additional-metric)
   - [Scenario A: Adding a Field to Existing Trackers (e.g., Reasoning Tokens, Memory Peak)](#scenario-a-adding-a-field-to-an-existing-tracker)
   - [Scenario B: Creating a Brand-New Custom Metric Tracker](#scenario-b-creating-a-brand-new-custom-metric-tracker)
   - [Step 1: Metric Capture & In-Memory State](#step-1-metric-capture--in-memory-state)
   - [Step 2: Cross-Stage & Cross-Pod Persistence (`save_to_file` & `load_and_merge`)](#step-2-cross-stage--cross-pod-persistence)
   - [Step 3: MLflow & OpenTelemetry Logging](#step-3-mlflow--opentelemetry-logging)
   - [Step 4: Instrumenting the Pipelines (Data Generation, Indexing, Analysis)](#step-4-instrumenting-the-pipelines)
   - [Step 5: Output Presentation (Console & Migration Report)](#step-5-output-presentation-console--migration-report)
   - [Step 6: Writing Unit Tests](#step-6-writing-unit-tests)
6. [Operational Environment Variables & Configurations](#operational-environment-variables--configurations)

---

## Overview & Purpose

The `token_metrics_telemetry` branch upgrades AUGUR with end-to-end **observability**, **cost governance**, and **latency profiling** across its three core pipelines:
1. **Data Generation** (code ingestion, language detection, LLM code metadata extraction)
2. **Indexing** (GraphRAG knowledge graph extraction and vector embedding generation)
3. **Analysis** (architectural dependency queries, report prompt generation, interactive visual graphs)

Before this update, pipeline durations and LLM token usage were opaque, making it difficult to detect pipeline bottlenecks, estimate execution costs, or track token consumption across distributed Kubeflow pods. This update introduces real-time token tracking, per-step duration measurement, MLflow metric logging, OpenTelemetry/Tempo distributed tracing, and automated summary tables embedded directly into the final `migration_report.md`.

---

## What Are the Current (Existing) Items?

To understand how the new telemetry integrates into the codebase, here is a summary of the baseline components that existed on `main`:

* **`pipelines/base/`**:
  * `data_generation.py`: Clones target Git repositories, parses source code, calls LLMs to extract metadata, and saves `.txt` and `_metadata.txt` files.
  * `indexing.py`: Takes the generated code files, configures GraphRAG, runs the indexing process, and evaluates the index against benchmark datasets.
  * `analysis.py`: Uses `DependencyAnalyzer` (`utils/graphrag_utils.py`) to execute LLM queries over the GraphRAG index and construct `migration_report.md`.
* **`pipelines/kubeflow/`**:
  * Containerized Kubeflow Pipelines (KFP) wrappers (`data_generation.py`, `indexing.py`, `analysis.py`) that run each pipeline stage in isolated Kubernetes pods with ephemeral volumes and artifact passing (`Dataset`, `Metrics`, `Markdown`).
* **`pipelines/orchestrator.py`**:
  * Orchestrates the three pipelines sequentially in single-repo or multi-repo mode, switching between direct execution and Kubeflow execution (`uses_kfp()`).
* **`loaders/` (`AssetLoader`)**:
  * `AssetLoader` base class, implemented by `LocalAssetLoader` (local file system) and `MlFlowAssetLoader` (MLflow artifact registry). Controlled by `DefaultAssetLoader` via the `ASSET_LOADER` environment variable.
* **`eval/` (`CustomEvaluator`)**:
  * Evaluator interface with `BasicCustomEvaluator` and `MlFlowCustomEvaluator` (LLM-as-judge benchmark), switched via `CUSTOM_EVALUATOR`.
* **`graphrag.sh`**:
  * A legacy shell script that invoked `python -m graphrag init` and `python -m graphrag index` in a sub-shell.

---

## What Are the New Items?

### 1. LLM Token Usage & Cost Tracking Subsystem
* **File**: [`workflows/examples/code_understanding/utils/token_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/token_tracker.py)
* **Core Class**: `TokenCostTracker`
* **Key Features**:
  * **Singleton Pattern**: Managed via `TokenCostTracker.get_instance()` and `reset_instance()`.
  * **Configurable Pricing**: Reads pricing dynamically from environment variables:
    * `CHAT_PRICE_PER_PROMPT_TOKEN` (default: `$0.000002` = $2.00 / 1M tokens)
    * `CHAT_PRICE_PER_OUTPUT_TOKEN` (default: `$0.000008` = $8.00 / 1M tokens)
    * `EMBED_PRICE_PER_PROMPT_TOKEN` (default: `$0.0000002` = $0.20 / 1M tokens)
  * **Automated Live Interception**:
    * **LiteLLM Callback Handler**: Intercepts `litellm.success_callback` to record tokens, response cost, and latency in real time.
    * **Direct OpenAI Client Monkey-Patching**: Transparently patches `AsyncCompletions.create`, `Completions.create`, `AsyncEmbeddings.create`, and `Embeddings.create` to capture calls made by third-party libraries (e.g. GraphRAG) that bypass LiteLLM.
  * **Heuristic & Parquet Extractors**:
    * `extract_graphrag_indexing_tokens`: Scans GraphRAG output files (`stats.json`, `text_units.parquet`, `community_reports.parquet`) to record tokens even if GraphRAG ran in a separate process or offline.
    * `extract_data_generation_tokens`: Reconstructs token counts from generated `_metadata.txt` and raw code files when direct callbacks are absent.
  * **Stage & Run Isolation**:
    * `merge(other, current_stage)` ensures that when downstream stages (e.g. Analysis) import upstream metrics (e.g. Indexing), existing records are not duplicated or overwritten.
  * **Reporting**:
    * Real-time console logs on every invocation: `[LLM Call] Source: ... | Calls: ... | Prompt Tokens: ... | Est. Cost: ...`
    * ASCII summary table via `format_summary()`.
    * Markdown summary via `format_markdown_section()` appended to `migration_report.md`.

---

### 2. Pipeline Step & Stage Duration Tracking Subsystem
* **File**: [`workflows/examples/code_understanding/utils/duration_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/duration_tracker.py)
* **Core Class**: `DurationTracker`
* **Key Features**:
  * **Singleton Pattern**: Managed via `DurationTracker.get_instance()` and `reset_instance()`.
  * **Context Manager (`measure`)**:
    ```python
    with dur_tracker.measure(stage="Data Generation", step="GitHub Checkout"):
        clone_from_repo(...)
    ```
    Automatically records elapsed time using `time.perf_counter()` and wall-clock timestamps (`start_time`, `end_time`), tagging status as `"success"` or `"failed"`.
  * **Decorator (`@track_duration`)**:
    ```python
    @track_duration(stage="Analysis", step="Analyze Dependencies")
    def analyze(...): ...
    ```
  * **Deduplication & Aggregate Awareness**:
    * Supports `metadata={"is_aggregate": True}` to track high-level stage totals without double-counting individual sub-steps in final totals.
  * **GraphRAG Duration Extractor**:
    * `extract_graphrag_indexing_durations`: Discovers GraphRAG's `stats.json` and records individual workflow durations (e.g., `GraphRAG: Extract Graph`, `GraphRAG: Generate Text Embeddings`).
  * **Reporting**:
    * **ASCII Table (`format_summary()`)**: Shows Stage, Step, Duration, and Status with stage percentages.
    * **Markdown Table (`format_markdown_table()`)**: Features Unicode latency bars (`████`), bottleneck identification (`Slowest Step:`), status badges, and deep links to active MLflow runs and artifacts.

---

### 3. In-Process GraphRAG Execution (`graphrag.py`)
* **File**: [`workflows/examples/code_understanding/pipelines/graphrag.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/graphrag.py) (Replaces legacy `graphrag.sh`)
* **Purpose**: Replaces the external shell script with direct Python API calls (`graphrag.cli.initialize.initialize_project_at` and `graphrag.api.build_index`).
* **Why It Matters**: Running GraphRAG in-process ensures that Python runtime hooks (LiteLLM callbacks, OpenAI patches, and `DurationTracker.measure`) can monitor GraphRAG's execution live in the same process memory space.

---

### 4. Telemetry Abstraction Layer (`telemetry/`)
* **Directory**: [`workflows/examples/code_understanding/telemetry/`](file:///c:/Dev/augur/workflows/examples/code_understanding/telemetry/)
* **Files**:
  * `custom_telemetry.py`: Abstract base class with `track()`.
  * `basic_custom_telemetry.py`: No-op implementation for environments where telemetry is disabled.
  * `mlflow_custom_telemetry.py`: Configures MLflow tracking URI, sets default experiment, enables `mlflow.openai.autolog()`, and binds LiteLLM callbacks.
  * `default_custom_telemetry.py`: Factory selecting between `MlFlowCustomTelemetry` and `BasicCustomTelemetry` based on `CUSTOM_TELEMETRY` or `CUSTOM_EVALUATOR` environment variables.
* **Helper**: [`workflows/examples/code_understanding/utils/otel_utils.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/otel_utils.py) provides the `@enable_telemetry` decorator.

---

### 5. Cross-Pod Telemetry Propagation (Kubeflow & Local)
In Kubeflow, the three pipelines run in separate pods:
1. **Data Generation Pod** executes and writes `target/durations.json` and `target/tokens.json`. In its `finally:` block, it logs metrics and artifacts to MLflow (`stage: "Data Generation"`).
2. **Indexing Pod** mounts the output dataset, searches for candidate telemetry files using `find_all_telemetry_files()`, and merges upstream Data Generation metrics. After indexing, it saves updated `durations.json` and `tokens.json` to the GraphRAG output and logs to MLflow (`stage: "Indexing"`).
3. **Analysis Pod** downloads upstream telemetry from MLflow or mounted inputs, captures its own step metrics, and injects the consolidated summary into `migration_report.md`. In its `finally:` block, it uploads final telemetry to MLflow.

---

### 6. OpenTelemetry & Tempo Tracing Infrastructure
* **Helm Templates**:
  * `resources/helm/templates/opentelemetry.yaml`: Deploys `OpenTelemetryCollector` and `TempoStack` custom resources on OpenShift.
  * `resources/helm/templates/create-tempo-bucket-job.yaml`: Pre-creates the S3/MinIO bucket for Tempo trace storage.
  * `resources/helm/templates/namespace.yaml` & `resources/helm/values.yaml`: Configures OTel namespaces and parameters.
* **Makefile**:
  * Added `deploy-otel` target that conditionally deploys the OpenTelemetry and Tempo stack if the OpenShift operators/CRDs are detected.
* **Environment Configuration**:
  * `.env.template` includes OTel exporter endpoints (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER=otlp_http`, `MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT=true`).

---

### 7. Test Suites
* **`workflows/examples/code_understanding/tests/test_token_tracker.py`**:
  * 40+ unit tests validating token counting, cost mathematics, pricing overrides, LiteLLM interceptors, OpenAI sync/async patches, parquet extractors, MLflow artifact uploads, and ASCII formatting.
* **`workflows/examples/code_understanding/tests/test_duration_tracker.py`**:
  * 30+ unit tests validating singleton behavior, `measure()` timing, `@track_duration`, aggregate step deduplication, MLflow metric logging, and Markdown latency bar generation.
* **`workflows/examples/code_understanding/tests/test_data_generation_telemetry.py`**:
  * End-to-end integration tests verifying telemetry preservation across simulated pipeline stages and failure handling in `finally:` blocks.

---

## File-by-File Changes Summary

| Area | File Path | Status | Key Changes |
| :--- | :--- | :---: | :--- |
| **Telemetry Core** | `utils/token_tracker.py` | **NEW** | `TokenCostTracker` singleton, LiteLLM callbacks, OpenAI patches, cost math, parquet extractors, MLflow upload/download. |
| **Telemetry Core** | `utils/duration_tracker.py` | **NEW** | `DurationTracker` singleton, `measure()` context manager, `@track_duration`, ASCII & Markdown latency tables, MLflow sync. |
| **Telemetry Core** | `telemetry/custom_telemetry.py` | **NEW** | Abstract base class for telemetry providers. |
| **Telemetry Core** | `telemetry/basic_custom_telemetry.py` | **NEW** | No-op telemetry provider. |
| **Telemetry Core** | `telemetry/default_custom_telemetry.py` | **NEW** | Telemetry provider factory. |
| **Telemetry Core** | `telemetry/mlflow_custom_telemetry.py` | **NEW** | MLflow autologging and LiteLLM callback setup. |
| **Telemetry Core** | `utils/otel_utils.py` | **NEW** | `@enable_telemetry` decorator. |
| **Pipelines (Base)** | `pipelines/base/data_generation.py` | **MODIFIED** | Instrumented steps (`Reset Environment`, `GitHub Checkout`, `LLM Metadata Extraction`, etc.), `finally:` blocks saving `tokens.json` & `durations.json`. |
| **Pipelines (Base)** | `pipelines/base/indexing.py` | **MODIFIED** | Upstream telemetry loading, instrumented steps (`Prepare Settings`, `GraphRAG Indexing Execution`, `Evaluate Index`), token extraction. |
| **Pipelines (Base)** | `pipelines/base/analysis.py` | **MODIFIED** | Upstream telemetry loading, markdown summary injection into `migration_report.md`, MLflow logging. |
| **Pipelines (Base)** | `pipelines/graphrag.py` | **NEW** | Replaces `graphrag.sh`. Runs GraphRAG in-process to allow live telemetry interception. |
| **Pipelines (Base)** | `pipelines/graphrag.sh` | **DELETED** | Replaced by in-process `graphrag.py`. |
| **Pipelines (KFP)** | `pipelines/kubeflow/data_generation.py` | **MODIFIED** | Added duration and token tracking to KFP component ops; logs summaries to pod stdout. |
| **Pipelines (KFP)** | `pipelines/kubeflow/indexing.py` | **MODIFIED** | Telemetry persistence and pod stdout summary logging in KFP indexing ops. |
| **Pipelines (KFP)** | `pipelines/kubeflow/analysis.py` | **MODIFIED** | Telemetry reporting in KFP analysis ops. |
| **Utilities** | `utils/graphrag_utils.py` | **MODIFIED** | Instrumented `query_with_llm`, per-prompt timing in `generate_migration_report`, and telemetry section injection into Markdown. |
| **Utilities** | `utils/kubeflow_utils.py` | **MODIFIED** | Updated `setup_logging()` to output directly to `sys.stdout` and remove `FileHandler`s so Kubernetes captures live metrics. |
| **Evaluators** | `eval/mlflow_custom_evaluator.py` | **MODIFIED** | Instrumented judge evaluations with token tracking. |
| **Infrastructure** | `resources/helm/templates/opentelemetry.yaml` | **NEW** | Helm template for TempoStack and OpenTelemetryCollector. |
| **Infrastructure** | `resources/helm/templates/create-tempo-bucket-job.yaml` | **NEW** | Helm template for S3 bucket creation job. |
| **Infrastructure** | `Makefile` | **MODIFIED** | Added `deploy-otel` target. |
| **Infrastructure** | `.env.template` | **MODIFIED** | Added OpenTelemetry configuration variables. |
| **Tests** | `tests/test_token_tracker.py` | **NEW** | 40+ unit tests for token tracker. |
| **Tests** | `tests/test_duration_tracker.py` | **NEW** | 30+ unit tests for duration tracker. |
| **Tests** | `tests/test_data_generation_telemetry.py` | **NEW** | End-to-end telemetry pipeline tests. |

---

## Step-by-Step Guide: How to Add an Additional Metric

Follow this guide if you need to add an additional metric to AUGUR (e.g., peak memory consumption, GPU utilization, cache hit counts, reasoning token counts, or custom validation scores).

---

### Scenario A: Adding a Field to an Existing Tracker

*Example: Adding `reasoning_tokens` to `TokenCostTracker` or `peak_memory_mb` to `DurationTracker`.*

#### 1. Update the In-Memory Record Schema
In `utils/token_tracker.py` (or `utils/duration_tracker.py`):
1. In `TokenCostTracker.track()`:
   * Accept the new parameter (e.g., `reasoning_tokens: int = 0`).
   * Add the field to `self.records[source]`:
     ```python
     if source not in self.records:
         self.records[source] = {
             "calls": 0,
             "prompt_tokens": 0,
             "output_tokens": 0,
             "reasoning_tokens": 0,  # <-- NEW
             "total_tokens": 0,
             "cost": 0.0,
         }
     self.records[source]["reasoning_tokens"] += reasoning_tokens
     ```
2. In `TokenCostTracker._extract_tokens()`:
   * Inspect the LLM response object or dictionary for the new metric:
     ```python
     # Example extracting from OpenAI completion_tokens_details
     details = getattr(usage, "completion_tokens_details", None)
     if details:
         reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0
     ```
3. Update `get_totals()`:
   * Aggregate the new metric across all sources:
     ```python
     total_reasoning = sum(r.get("reasoning_tokens", 0) for r in self.records.values())
     return {
         ...
         "total_reasoning_tokens": total_reasoning,
     }
     ```
4. Update `to_dict()` and `from_dict()` so the field is preserved across JSON serialization.
5. In `log_to_mlflow()`:
   * Add the metric to the logged dictionary:
     ```python
     metrics["llm_total_reasoning_tokens"] = totals["total_reasoning_tokens"]
     metrics[f"llm_{source_clean}_reasoning_tokens"] = r.get("reasoning_tokens", 0)
     ```
6. In `format_summary()`:
   * Add a row or column to the ASCII and Markdown tables.

---

### Scenario B: Creating a Brand-New Custom Metric Tracker

*Example: Creating a `SystemResourceTracker` to track CPU, RAM, or GPU usage per pipeline step.*

#### Step 1: Metric Capture & In-Memory State
Create a new utility module, e.g. `workflows/examples/code_understanding/utils/resource_tracker.py`:

```python
import os
import json
import logging
from typing import Dict, Any, List, Optional

class ResourceTracker:
    """Tracks resource consumption metrics (memory, GPU) across pipeline steps."""

    _global_instance: Optional["ResourceTracker"] = None

    @classmethod
    def get_instance(cls) -> "ResourceTracker":
        if cls._global_instance is None:
            cls._global_instance = cls()
        return cls._global_instance

    @classmethod
    def reset_instance(cls) -> "ResourceTracker":
        cls._global_instance = cls()
        return cls._global_instance

    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    def record(self, stage: str, step: str, peak_memory_mb: float, metadata: Optional[Dict] = None):
        self.records.append({
            "stage": stage,
            "step": step,
            "peak_memory_mb": float(peak_memory_mb),
            "metadata": metadata or {},
        })
```

#### Step 2: Cross-Stage & Cross-Pod Persistence
Implement JSON serialization and stage-aware merging:

```python
    def to_dict(self) -> Dict[str, Any]:
        return {"records": self.records}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceTracker":
        tracker = cls()
        tracker.records = list(data.get("records", []))
        return tracker

    def save_to_file(self, filepath: str):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    def load_and_merge(self, filepath: str, current_stage: Optional[str] = None):
        if not os.path.exists(filepath):
            return
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        other = ResourceTracker.from_dict(data)
        # Avoid duplicating records from the current stage
        for rec in other.records:
            if current_stage and rec.get("stage") == current_stage:
                continue
            if rec not in self.records:
                self.records.append(rec)
```

#### Step 3: MLflow & OpenTelemetry Logging
Add methods to upload metrics and artifacts to MLflow:

```python
    def log_to_mlflow(self, run_id: Optional[str] = None):
        try:
            import mlflow
            metrics = {
                f"resource_{r['stage'].lower()}_{r['step'].lower()}_mem_mb": r["peak_memory_mb"]
                for r in self.records
            }
            active = mlflow.active_run()
            if active:
                mlflow.log_metrics(metrics)
            elif run_id:
                with mlflow.start_run(run_id=run_id):
                    mlflow.log_metrics(metrics)
        except Exception as e:
            logging.debug(f"Failed to log resources to MLflow: {e}")

    def upload_to_mlflow(self, git_slug: Optional[str] = None, stage: Optional[str] = None):
        import tempfile, shutil
        tmp_dir = tempfile.mkdtemp()
        try:
            tmp_file = os.path.join(tmp_dir, "resources.json")
            self.save_to_file(tmp_file)
            from loaders.default_asset_loader import DefaultAssetLoader
            artifact_path = DefaultAssetLoader.get_log_results_artifact_path(
                DefaultAssetLoader.RESULTS_PATH_PREFIX_TELEMETRY, git_slug=git_slug
            )
            DefaultAssetLoader().log_results(
                tmp_file,
                artifact_path=artifact_path,
                tags={"category": "telemetry", "type": "resources", "stage": str(stage or "")}
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
```

#### Step 4: Instrumenting the Pipelines
Wire your tracker into the three pipeline stages:

1. **In `pipelines/base/data_generation.py`**:
   * In `run()`: Initialize tracker and record metric per step.
   * In `finally:` block: Call `save_to_file(os.path.join(target_path, "resources.json"))` and `upload_to_mlflow(git_slug=git_slug, stage="Data Generation")`.
2. **In `pipelines/base/indexing.py`**:
   * Near the start: Discover candidate files with `find_all_telemetry_files(candidate_dirs, "resources.json")` and call `load_and_merge(f, current_stage="Indexing")`.
   * In `finally:` block: Save to `graphrag_source_path/resources.json` and upload to MLflow.
3. **In `pipelines/base/analysis.py`**:
   * Near the start: Merge upstream `resources.json` files.
   * In `finally:` block: Upload consolidated metrics to MLflow.
4. **In Kubeflow components (`pipelines/kubeflow/*.py`)**:
   * Add `save_to_file` and `upload_to_mlflow` inside the pod's `finally:` block so intermediate artifacts are passed along to subsequent pipeline pods.

#### Step 5: Output Presentation (Console & Migration Report)
1. Add a formatting method `format_markdown_section() -> str`:
   ```python
   def format_markdown_section(self) -> str:
       lines = ["\n### System Resource Summary\n", "| Stage | Step | Peak Memory (MB) |", "| :--- | :--- | :---: |"]
       for r in self.records:
           lines.append(f"| {r['stage']} | {r['step']} | {r['peak_memory_mb']:.1f} MB |")
       return "\n".join(lines) + "\n"
   ```
2. In [`workflows/examples/code_understanding/utils/graphrag_utils.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/graphrag_utils.py) in `generate_migration_report()`:
   * Inject your formatted section into `report`:
     ```python
     resource_section = ResourceTracker.get_instance().format_markdown_section()
     report += f"\n\n{resource_section}"
     ```

#### Step 6: Writing Unit Tests
Add unit tests in `workflows/examples/code_understanding/tests/`:
* Test singleton initialization and reset (`test_singleton_get_and_reset`).
* Test recording and accumulation math.
* Test JSON serialization (`to_dict` / `from_dict`).
* Test file merge behavior and stage exclusion.
* Test mock MLflow metric logging.
* Run tests with:
  ```bash
  python -m unittest discover -s workflows/examples/code_understanding/tests -p "test_*.py"
  ```

---

## Operational Environment Variables & Configurations

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `CHAT_PRICE_PER_PROMPT_TOKEN` | `0.000002` | USD cost per input prompt token for chat LLMs ($2.00 / 1M). |
| `CHAT_PRICE_PER_OUTPUT_TOKEN` | `0.000008` | USD cost per completion/output token for chat LLMs ($8.00 / 1M). |
| `EMBED_PRICE_PER_PROMPT_TOKEN` | `0.0000002` | USD cost per input token for embedding models ($0.20 / 1M). |
| `TOKEN_TRACKER_PRINT_CONSOLE` | `true` | When `true`, prints a live log to stdout on every LLM call. |
| `CUSTOM_TELEMETRY` | `basic` | Set to `mlflow` to enable MLflow autologging and telemetry callbacks. |
| `CUSTOM_EVALUATOR` | `basic` | Set to `mlflow` to enable MLflow GenAI evaluation metrics. |
| `MLFLOW_TRACKING_URI` | *None* | URI for the MLflow tracking server (e.g. `http://mlflow-server:5000`). |
| `MLFLOW_EXPERIMENT_NAME` | `AIP-default` | Default MLflow experiment name for telemetry runs. |
| `OTEL_SERVICE_NAME` | `code-understanding` | Service name reported to OpenTelemetry and Tempo collector. |
| `OTEL_NAMESPACE` | *None* | Kubernetes namespace where Tempo and OpenTelemetry collector reside. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`| *Cluster URL* | OTLP exporter endpoint (e.g. `http://...:4318`). |
| `OTEL_EXPORTER` | `otlp_http` | Exporter transport protocol (`otlp_http` or `otlp_grpc`). |
| `LOGLEVEL` | `INFO` | Python logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
