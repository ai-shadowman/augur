# AUGUR: Code Understanding Workflow

This directory contains the core pipelines and components for AUGUR's Code Understanding workflow:
- **`pipelines/base/`**: Base Python implementations of the three pipeline stages:
  1. `data_generation.py` (Source ingestion, language detection, LLM code & config metadata extraction)
  2. `indexing.py` (GraphRAG knowledge graph extraction and vector embedding generation)
  3. `analysis.py` (Dependency extraction, architectural analysis, and migration report generation)
- **`pipelines/kubeflow/`**: Kubeflow Pipelines (KFP) wrappers for containerized, distributed execution across Kubernetes pods.
- **`pipelines/graphrag.py`**: In-process GraphRAG execution runner replacing legacy shell scripts.
- **`utils/`**: Core utilities including:
  - [`token_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/token_tracker.py): Real-time token usage and cost estimation singleton.
  - [`duration_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/duration_tracker.py): Per-step pipeline duration measurement and latency profiling.
  - [`graphrag_utils.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/graphrag_utils.py): GraphRAG querying, dependency analysis, and markdown report synthesis.
- **`telemetry/`**: Pluggable telemetry abstraction layer (`CustomTelemetry`, `MlFlowCustomTelemetry`, `BasicCustomTelemetry`).
- **`loaders/`**: Pluggable asset loaders for local filesystem and MLflow artifact registries.
- **`eval/`**: Benchmark evaluation and LLM-as-judge scoring.
- **`tests/`**: Comprehensive unit tests for telemetry, token cost tracking, durations, and pipeline execution.

---

## Repository exclusions with `.augurignore`

To exclude repository content from Augur analysis, commit an optional
`.augurignore` file at the root of the repository before running Augur. Augur
reads only `<repository-root>/.augurignore` from the selected remote branch;
nested files and runtime patterns are unsupported.

The file uses Git-compatible ignore rules through `pathspec`, including ordered
rules, wildcards, directory rules, and negation. Its exclusions are additive to
Augur's built-in exclusions: a negated rule cannot re-include a file or
directory that Augur already excludes. If the file is absent, empty, or
comment-only, existing behavior is unchanged.

Augur must clone the repository before it can discover this file, so its rules
do not reduce clone or download contents. Once loaded, ignored paths are
skipped during language detection, metadata loading, and dataset generation.
A present file that cannot be read as UTF-8 or compiled fails that repository
before analysis rather than proceeding without the requested exclusions.

Normal logs report whether the root file was found, its active-pattern count,
the active patterns in file order, and aggregate excluded file and directory
counts. Individual excluded paths are logged only at debug level.

```gitignore
# Generated fixtures
tests/fixtures/generated/

# Large captured payloads
**/*.har

# Ignore reports except the maintained example
reports/*
!reports/example.md
```

## Telemetry & Metrics Maintenance Guide

For full architectural details on LLM token metrics, pipeline duration tracking, cross-pod artifact propagation, and step-by-step instructions on **how to add an additional metric**, see the project root maintainer guide:

👉 **[README_METRICS.md](../../../README_METRICS.md)**
