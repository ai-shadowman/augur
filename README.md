# Agent Mesh for Software Modernization - Code Understanding

Contents
---
- [ ] [Overview](#overview)
- [ ] [Required Software / Tested with](#tested-with)
- [ ] [Installing the Code Understanding Workflow](#installing-the-code-understanding-workflow)
  - [ ] [Integrating the Models](#integrating-the-models)
  - [ ] [Preparing the Environment](#preparing-the-environment)
  - [ ] [(Optional) Building the Container Images](#optional-building-the-container-images)
  - [ ] [Installing via Makefile](#installing-via-makefile)
- [ ] [Running the Code Understanding Workflow](#running-the-code-understanding-workflow)
- [ ] [More About the Code Understanding Workflow](#more-about-the-code-understanding-workflow)
  - [ ] [1. Data Generation](#1-data-generation)
  - [ ] [2. Data Indexing](#2-data-indexing)
  - [ ] [3. Data Analysis](#3-data-analysis)

<a id="overview"></a>
## 🧭 Overview

[![Watch the demo](https://drive.google.com/thumbnail?id=18FxSpwKBPPgzQnP1Q9ToPC_kLskrnni3&sz=w1280)](https://youtu.be/swRB1iAyGr0)
*▶ Click to watch the demo*

This demonstrates the **Code Understanding** phase of the Agent Mesh for Software Engineering, a framework pattern 
for continuous legacy code which uses a federated, multi-harness, multi-agent 
system (MAS) to support iterative agent-driven development for brownfield applications.

(NOTE: The Agent Mesh consists of two main **workflows**: **Code 
Understanding** and **Code Migration**. This repository demonstrates the **Code Understanding** workflow.)

<a id="tested-with"></a>
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

## Installing the Code Understanding Workflow

### Integrating the Models
Ensure that you have access to OpenAI-compatible endpoints for the 
following models:

1. GraphRAG "chat" model 
(see <a href="https://microsoft.github.io/graphrag/config/yaml" target="_blank">docs</a>). Candidate models: 
- gpt-oss-120b (see: `https://huggingface.co/RedHatAI/gpt-oss-120b`)
```
#################################
# Sample vLLM Deployment on H100:
#################################
export HF_TOKEN=<your-huggingface-token>
export VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
pip install vllm==0.19.0 mistral-common==1.9.1 tqdm==4.67.3 jupyter==1.1.1 hf_transfer==0.1.9 transformers==4.55.2
nohup python3 -m vllm.entrypoints.openai.api_server \
    --model RedHatAI/gpt-oss-120b \ 
    --enable-auto-tool-choice \
    --tool-call-parser openai \                                                                                    
    --max-model-len 128000 \
    > vllm.log 2>&1 &
```

2. GraphRAG "embedding" model 
(see <a href="https://microsoft.github.io/graphrag/config/yaml" target="_blank">docs</a>). Candidate models:
- e5-mistral-7b-instruct (see: `https://huggingface.co/intfloat/e5-mistral-7b-instruct`)
```
#################################
# Sample vLLM Deployment on L40S:
#################################
export HF_TOKEN=<your-huggingface-token>
pip install vllm==0.19.0 mistral-common==1.9.1 tqdm==4.67.3 jupyter==1.1.1 hf_transfer==0.1.9 transformers==4.55.2
nohup python -m vllm.entrypoints.openai.api_server \
--model=intfloat/e5-mistral-7b-instruct \                                                                  
--runner pooling \  # or --task=embed for older vllm                                                                                       
--dtype float16 
> vllm.log 2>&1 &
```
   
3. Coding agent model (for invoking skills). Candidate models:
- Gemma-4-31B-it (see: `https://huggingface.co/RedHatAI/gemma-4-31B-it-NVFP4`)
```
#################################
# Sample vLLM Deployment on H200:
#################################
pip install vllm==0.19.0 tqdm==4.67.3 jupyter==1.1.1 hf_transfer==0.1.9 huggingface-hub "transformers<5.0.0,>=4.56.0"
pip install huggingface-hub==1.14.0 transformers==5.8.0
wget https://huggingface.co/RedHatAI/gemma-4-31B-it-NVFP4/blob/main/chat_template.jinja
nohup python3 -m vllm.entrypoints.openai.api_server \
     --model RedHatAI/gemma-4-31B-it-NVFP4 \
     --quantization fp8 \
     --kv-cache-dtype fp8 \
     --enable-auto-tool-choice \
     --reasoning-parser gemma4 \
     --tool-call-parser gemma4 \
     --chat-template chat_template.jinja \
     --gpu-memory-utilization 0.90 \
     --max-model-len 262144 \
      > vllm.log 2>&1 &
```
- gpt-oss-120b (see: (a) above)

### Preparing the Environment

1. Create an environment variables file `.env` using `.env-template` as a guide.

### (Optional) Building the Container Images
1. To build the container images, run the following: `make build-images`

### Installing via Makefile
1. Run the Makefile: `make install`

## Running the Code Understanding Workflow
1. To run the **Code Understanding** pipeline for a single repository, run:
```make run-pipelines ARGS="--single"```

2. To run the **Code Understanding** pipeline for multiple repositories, 
   perform the following steps:
    - Update `assets/repos/repo_list.json` with the list of repositories to 
      be processed.
    - Run the following command:
   ```make run-pipelines ARGS="--aggregated"```

## Running Adhoc Queries
1. To run adhoc queries about the indexed code, run the following:
```shell/adhoc.sh <query>```
   - For example: 
     - `shell/adhoc.sh "What migration order would be recommended when refactoring to reduce breaking changes? Include the fully qualified names."`
     - `shell/adhoc.sh "Which modules or components would be riskiest to refactor first? Include the fully qualified names."`
     - `shell/adhoc.sh "Which modules or components would be the least risky to refactor first? Include the fully qualified names."`

## More About the Code Understanding Workflow

![High Level Overview](code_understanding.jpg)

The **Code Understanding** workflow is the initial **iteration** in the 
process. It is a **tool-driven workflow** which generates artifacts for the 
**refactoring catalog**. These artifacts are optionally combined with 
other tools (organizational vulnerability scanners, static rules engines, 
etc) to build the **migration plan** for the **Code Migration** workflow.

There are three main sub-workflows in the **Code Understanding** workflow:

#### 1. Data Generation

The **Data Generation** sub-workflow is used to generate metadata that will be 
used for GraphRAG-based indexing. For each relevant file in the original 
codebase, it will generate a `.txt` version of the file and a new metadata 
file. This enriched fileset will then be passed as input to the **Data Indexing** workbench in the next step.

#### 2. Data Indexing

The **Data Indexing** sub-workflow is used to index the fileset from the 
**Data Generation** step using GraphRAG. It will generate a graph-based 
representation of the codebase that can be used for querying.

#### 3. Data Analysis

The **Data Analysis** workbench is used to query the generated GraphRAG index using the GraphRAG SDK.
It includes both canned and adhoc queries that can be used to explore the 
code and generate assets for the refactoring catalog, including a migration plan.


