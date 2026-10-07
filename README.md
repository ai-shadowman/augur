# AUGUR: **A**gentic **U**nderstanding for **G**uided **U**pgrade **R**ecommendations

![AUGUR](media/AUGUR.png)

## Overview

**AUGUR** (**A**gentic **U**nderstanding for **G**uided **U**pgrade **R**ecommendations). It serves as the "Code Understanding" phase of a future larger multi-agent system designed to support iterative, agent-driven development for modernizing legacy software ("brownfield applications").

### What It Does

The primary goal of AUGUR is to deeply analyze existing codebases and generate strategic insights to plan for software migration and refactoring. Specifically, it:

* Generates architectural artifacts to build a refactoring catalog and a structured high-level migration plan.

* Allows developers to execute ad-hoc, natural-language queries against the Graph-RAG representation of AUGUR's knowledge of the codebase. For example, users can ask the system to identify the data stores, determine which modules are the riskiest to refactor, or recommend an upgrade order to minimize breaking changes.

### How It Works

AUGUR executes its code understanding workflow through a three-step pipeline:

1. **Data Generation:** AUGUR scans the target codebase, generating raw text versions and metadata for each relevant file. It can also merge output from external tools—like vulnerability scanners, static code parsers, and dependency analyzers—into this dataset to enrich the context.

2. **Data Indexing:** The file set and metadata are ingested into **GraphRAG** (**Graph** **R**etrieval-**A**ugmented **G**eneration). This process indexes the data to create a comprehensive, graph-based representation of the codebase, mapping out how different components and functions relate to one another.

3. **Data Analysis:** AUGUR queries the generated GraphRAG index using the GraphRAG SDK and Large Language Models (LLMs). By running both canned and custom queries, the agents explore the code graph to generate the final refactoring assets and migration recommendations.

### Technology Stack

To power this intensive process, AUGUR relies on an enterprise-grade, AI-accelerated infrastructure:

* **Platform:** AUGUR is designed to be deployed on Red Hat OpenShift and Red Hat OpenShift AI using dedicated GPU worker nodes (such as NVIDIA H100, A100, or L40 instances).

* **AI Models:** AUGUR federates different tasks to specialized models, including a GraphRAG "chat" model (e.g., gpt-oss-120b), an "embedding" model for indexing (e.g., e5-mistral-7b-instruct), etc...

* **Tooling:** AUGUR utilizes MLflow for evaluation/tracking, MinIO for S3-compatible data storage, and OpenShift Pipelines to automate the workflow.

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

## Documentation
- [Logging Architecture](README_LOGGING.md) - Unified console logging and environment configuration
- [Telemetry & Metrics Guide](README_METRICS.md) - Token and duration tracking architecture

***TODO***
