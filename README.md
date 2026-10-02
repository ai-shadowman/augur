# WIP: AUGUR: Agentic Understanding for Guided Upgrade Recommendations

![AUGUR](media/AUGUR.png)

## Overview

**AUGUR (Agentic Understanding for Guided Upgrade Recommendations)** is an AI-driven codebase understanding and architectural modernization platform designed to run natively in **Red Hat OpenShift AI Pipelines** (Kubeflow Pipelines / Tekton / Argo) and local environments.

AUGUR automates the analysis of legacy repositories to produce actionable modernization and migration roadmaps. It ingests one or more Git repositories, extracts language-specific AST and documentation metadata, builds a comprehensive **GraphRAG (Graph-based Retrieval-Augmented Generation)** knowledge graph with vector embeddings, identifies architectural layers and dependencies, and produces a complete **Migration Report** (`migration_report.md`) with a machine-readable JSON migration plan.

### Core Pipelines
1. **Data Generation**: Clones repositories, detects programming languages, calculates repository size & lines of code (SLOC), and extracts structured code/config metadata using LLMs.
2. **Indexing**: Ingests generated code metadata into an in-process GraphRAG engine, building entity-relationship graphs, hierarchical Leiden communities, and vector embeddings.
3. **Analysis**: Queries the GraphRAG index to map dependency layers, generate interactive visualizations (PyVis/NetworkX), and synthesize migration recommendations and modernization roadmaps.

### Comprehensive Observability & Telemetry
AUGUR features end-to-end telemetry embedded directly into its execution reports and MLflow runs:
- **Codebase Scope & Size Metrics**: Files, SLOC, comments, blanks, and config metrics tracked via [`CodeMetricsTracker`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/code_metrics_tracker.py). See 👉 **[README_CODE_METRICS.md](README_CODE_METRICS.md)**.
- **LLM Token Usage & Cost Governance**: Real-time prompt/completion token tracking and dollar cost estimation via [`TokenCostTracker`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/token_tracker.py). See 👉 **[README_METRICS.md](README_METRICS.md)**.
- **Pipeline Step Latency Profiling**: High-precision wall-clock latency profiling via [`DurationTracker`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/duration_tracker.py). See 👉 **[README_METRICS.md](README_METRICS.md)**.
- **Distributed Tracing**: OpenTelemetry SDK integration exporting traces to Red Hat OpenShift distributed tracing (Tempo).

## Required Software / Tested with

- Red Hat OpenShift 4.18+
- Red Hat OpenShift AI 2.22+
- 1X NVIDIA H200 GPU, 1X NVIDIA H100 GPU, 1X NVIDIA L40S GPU
- 8+ vCPUs / 24+ GiB RAM
- MLflow (assumes Openshift AI 3.4+) [Installation](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.4/html/working_with_mlflow/installing-mlflow_mlflow)
- Openshift AI Model Registry [Installation](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/2.25/html-single/enabling_the_model_registry_component/index)
- Openshift AI Model Catalog [Installation](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.4/html-single/working_with_the_model_catalog/index)
- Openshift AI Pipelines [Installation](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/openshift_ai_tutorial_-_fraud_detection_example/setting-up-a-project-and-storage#enabling-ai-pipelines)
- OpenShift CLI (`oc`)
- Helm CLI (`helm`)
- Make (`make`)

## Disconnected / Air-Gapped Environment Considerations

In air-gapped or disconnected OpenShift clusters without public internet access, external runtime dependency downloads will fail. 

### Tiktoken Tokenizer Cache
OpenAI's `tiktoken` library (used by GraphRAG for chunking, token counting, and prompt context budgeting) dynamically downloads its BPE vocabulary file from `https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken` on first use. In a disconnected environment, this causes container pods to hang or crash with `urllib.error.URLError`.

To prevent external network access at runtime, the `cl100k_base.tiktoken` file is pre-downloaded and baked into each pipeline container image under `/opt/app-root/src/tiktoken_cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4` alongside the `TIKTOKEN_CACHE_DIR` environment variable.

For complete build instructions and hashing details, see:
👉 **[resources/images/README.md](resources/images/README.md#offline--disconnected-support-tiktoken-cache)**

## Quickstart & Usage

### Running Locally
To run AUGUR standalone on a target repository without Kubeflow:
```bash
export GIT_REPO="https://github.com/my-org/my-service"
export GIT_BRANCH="main"
export GRAPHRAG_LLM_API_BASE="http://vllm-endpoint:8000/v1"
export GRAPHRAG_LLM_MODEL="openai/gpt-oss-120b"
export EMBED_LLM_API_BASE="http://vllm-endpoint:8000/v1"
export EMBED_LLM_MODEL="e5-mistral-7b-instruct"

# Run all 3 stages locally (Data Generation -> Indexing -> Analysis)
python workflows/examples/code_understanding/pipelines/orchestrator.py
```

### Running in OpenShift AI Pipelines (Kubeflow)
Deploy Helm templates and compile pipelines to YAML:
```bash
make install
```
Or compile pipelines standalone:
```bash
PIPELINE_COMPILE_ONLY=1 \
KFP_PIPELINE_OUTPUT_DIR=compiled_pipelines \
PYTHONPATH=workflows/examples/code_understanding \
python workflows/examples/code_understanding/pipelines/orchestrator.py
```

### Running Test Suites
AUGUR includes an automated unit testing suite across all telemetry and pipeline components:
```bash
python -m unittest discover -s workflows/examples/code_understanding/tests
```

## Documentation & Guides
- 👉 **[README_CODE_METRICS.md](README_CODE_METRICS.md)**: Detailed guide for `CodeMetricsTracker` (files, lines of code, comments, blanks).
- 👉 **[README_METRICS.md](README_METRICS.md)**: Telemetry architecture for LLM token usage, duration latency tracking, and metric extensibility.
- 👉 **[resources/images/README.md](resources/images/README.md)**: Container image build process and disconnected/air-gapped registry mirroring.
- 👉 **[workflows/examples/code_understanding/README.md](workflows/examples/code_understanding/README.md)**: Code understanding package overview.
