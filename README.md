# WIP: AUGUR: Agentic Understanding for Guided Upgrade Recommendations

![AUGUR](media/AUGUR.png)

## Overview

***TODO***

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
