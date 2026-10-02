# AUGUR: Code Understanding Workflow

This directory contains the core pipelines and components for AUGUR's Code Understanding workflow:
- **`pipelines/base/`**: Base Python implementations of the three pipeline stages:
  1. `data_generation.py` (Source ingestion, language detection, LLM code & config metadata extraction)
  2. `indexing.py` (GraphRAG knowledge graph extraction and vector embedding generation)
  3. `analysis.py` (Dependency extraction, architectural analysis, and migration report generation)
- **`pipelines/kubeflow/`**: Kubeflow Pipelines (KFP) wrappers for containerized, distributed execution across Kubernetes pods.
- **`pipelines/graphrag.py`**: In-process GraphRAG execution runner replacing legacy shell scripts.
- **`utils/`**: Core utilities including:
  - [`code_metrics_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/code_metrics_tracker.py): Repository scope, file count, and lines of code (SLOC) measurement singleton.
  - [`token_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/token_tracker.py): Real-time token usage and cost estimation singleton.
  - [`duration_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/duration_tracker.py): Per-step pipeline duration measurement and latency profiling.
  - [`graphrag_utils.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/graphrag_utils.py): GraphRAG querying, dependency analysis, and markdown report synthesis.
- **`telemetry/`**: Pluggable telemetry abstraction layer (`CustomTelemetry`, `MlFlowCustomTelemetry`, `BasicCustomTelemetry`).
- **`loaders/`**: Pluggable asset loaders for local filesystem and MLflow artifact registries.
- **`eval/`**: Benchmark evaluation and LLM-as-judge scoring.
- **`tests/`**: Comprehensive unit tests for telemetry, token cost tracking, durations, code metrics, and pipeline execution.

---

## Telemetry & Metrics Maintenance Guides

For architectural details and implementation guides across the telemetry subsystems:

- 👉 **[README_CODE_METRICS.md](../../../README_CODE_METRICS.md)**: Repository scope, file count & lines-of-code tracking guide.
- 👉 **[README_METRICS.md](../../../README_METRICS.md)**: LLM token usage, duration tracking, cross-pod artifact propagation, and metrics extensibility guide.
