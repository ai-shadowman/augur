# Tola's Harness Installation for Augur

Configures the Augur `.env` from the cluster state and runs the pipelines.

**Prerequisite:** OpenShift cluster must be provisioned and configured with GPU worker, AI Accelerator, MinIO, and embedding model. See [OpenShift Cluster Setup](openshift-cluster-setup-digital-twin.md) for complete cluster installation instructions.

> ⚠️ **HERE BE DRAGONS**

> **Prerequisites:** `GIT_TOKEN` and `GPT_LLM_TOKEN` are intentionally not set in the snippets below (redacted / commented out) — export both with your own values before running the `sed` lines that reference them.

## Contents

1. [Tola's Harness](#1-tolas-harness)

---

## 1. Tola's Harness

### Project setup and clone

```bash
DATA_SCIENCE_PROJECT_NAME="codearcheology"
MINIO_PROJECT_NAME="openshiftai-minio"

oc new-project $DATA_SCIENCE_PROJECT_NAME

GIT_AUGUR_BRANCH="main"

git clone -b $GIT_AUGUR_BRANCH git@github.com:ai-shadowman/augur.git

cd augur/

cp .env.template .env
```

### Set Git credentials

```bash
GIT_USERNAME="kfrankli"
#GIT_TOKEN="REDACTED"

GIT_BRANCH="main"

#GIT_REPO="https://github.com/jboss-developer/ticket-monster"
#GIT_REPO="https://github.com/agapebondservant/spacefx-sample-javafx"
GIT_REPO="https://github.com/agapebondservant/tic-tac-toe-sample"

sed -i "s|GIT_USERNAME=.*|GIT_USERNAME=$GIT_USERNAME|" .env
sed -i "s|GIT_TOKEN=.*|GIT_TOKEN=$GIT_TOKEN|" .env
sed -i "s|GIT_REPO=.*|GIT_REPO=$GIT_REPO|" .env
sed -i "s|GIT_BRANCH=.*|GIT_BRANCH=$GIT_BRANCH|" .env
```

### Set S3 bucket settings

Pull the MinIO root credentials deployed in [step 4 of the cluster setup guide](openshift-cluster-setup-digital-twin.md#4-deploy-minio):

```bash
MINIO_ROOT_USER=$(echo "$(oc get secret minio-secret -n openshiftai-minio -o jsonpath='{.data.minio_root_user}')" | base64 -d)
MINIO_ROOT_PASSWORD=$(echo "$(oc get secret minio-secret -n openshiftai-minio -o jsonpath='{.data.minio_root_password}')" | base64 -d)

sed -i "s/AWS_ACCESS_KEY_ID=.*/AWS_ACCESS_KEY_ID=$MINIO_ROOT_USER/" .env
sed -i "s/AWS_SECRET_ACCESS_KEY=.*/AWS_SECRET_ACCESS_KEY=$MINIO_ROOT_PASSWORD/" .env
```

### Set GraphRAG providers

```bash
#sed -i "s/GRAPHRAG_LLM_PROVIDER_GRAPHRAG=.*/GRAPHRAG_LLM_PROVIDER_GRAPHRAG=hosted_vllm/" .env

sed -i "s/GRAPHRAG_LLM_PROVIDER_SETTINGS_XML=.*/GRAPHRAG_LLM_PROVIDER_SETTINGS_XML=hosted_vllm/" .env

sed -i "s/EMBED_LLM_PROVIDER_GRAPHRAG=.*/EMBED_LLM_PROVIDER_GRAPHRAG=hosted_vllm/" .env
sed -i "s/JUDGE_LLM_PROVIDER_GRAPHRAG=.*/JUDGE_LLM_PROVIDER_GRAPHRAG=hosted_vllm/" .env
sed -i "s/GROUND_TRUTH_LLM_PROVIDER_GRAPHRAG=.*/GROUND_TRUTH_LLM_PROVIDER_GRAPHRAG=hosted_vllm/" .env

sed -i "s/EMBED_LLM_PROVIDER_SETTINGS_XML=.*/EMBED_LLM_PROVIDER_SETTINGS_XML=openai/" .env
```

### Set LLM providers

Provider names follow the [LiteLLM provider name standards](https://docs.litellm.ai/docs/providers/vllm).

```bash
sed -i "s/GRAPHRAG_LLM_PROVIDER=.*/GRAPHRAG_LLM_PROVIDER=openai/" .env
sed -i "s/EMBED_LLM_PROVIDER=.*/EMBED_LLM_PROVIDER=hosted_vllm/" .env
sed -i "s/GROUND_TRUTH_LLM_PROVIDER=.*/GROUND_TRUTH_LLM_PROVIDER=hosted_vllm/" .env
sed -i "s/JUDGE_LLM_PROVIDER=.*/JUDGE_LLM_PROVIDER=hosted_vllm/" .env
sed -i "s/CODE_LLM_PROVIDER=.*/CODE_LLM_PROVIDER=hosted_vllm/" .env
```

### Set model IDs

```bash
# The name appears several times on the inferenceservices object; this may be the wrong spot to pull name
MISTRAL_RUNTIME_NAME=$(oc get inferenceservices.serving.kserve.io e5-mistral-7b-instruct -n model-e5-mistral-7b-instruct -o jsonpath='{.spec.predictor.model.runtime}')

#GPT_LLM_RUNTIME_NAME=$(oc get inferenceservices.serving.kserve.io redhataigpt-oss-120b -n model-gpt-oss-120b -o jsonpath='{.spec.predictor.model.runtime}')

#GPT_LLM_RUNTIME_NAME="gpt-oss-120b"

GPT_LLM_RUNTIME_NAME="Qwen3.6-35B-A3B"

sed -i "s/GRAPHRAG_LLM_ID=.*/GRAPHRAG_LLM_ID=$GPT_LLM_RUNTIME_NAME/" .env
sed -i "s/EMBED_LLM_ID=.*/EMBED_LLM_ID=$MISTRAL_RUNTIME_NAME/" .env
sed -i "s/GROUND_TRUTH_LLM_ID=.*/GROUND_TRUTH_LLM_ID=$GPT_LLM_RUNTIME_NAME/" .env
sed -i "s/JUDGE_LLM_ID=.*/JUDGE_LLM_ID=$GPT_LLM_RUNTIME_NAME/" .env
sed -i "s/CODE_LLM_ID=.*/CODE_LLM_ID=$GPT_LLM_RUNTIME_NAME/" .env
```

### Set API base URLs

```bash
MISTREL_API_BASE=$(oc get inferenceservices.serving.kserve.io e5-mistral-7b-instruct -n model-e5-mistral-7b-instruct -o jsonpath='{.status.address.url}')/v1

#GPT_LLM_API_BASE=$(oc get inferenceservices.serving.kserve.io redhataigpt-oss-120b -n model-gpt-oss-120b -o jsonpath='{.status.address.url}')/v1

#GPT_LLM_API_BASE="https://maas-rhdp.apps.maas.redhatworkshops.io/v1"
GPT_LLM_API_BASE="https://litemaas.rhoai.rh-aiservices-bu.com/v1"

sed -i "s|GRAPHRAG_LLM_API_BASE=.*|GRAPHRAG_LLM_API_BASE=$GPT_LLM_API_BASE|" .env
sed -i "s|EMBED_LLM_API_BASE=.*|EMBED_LLM_API_BASE=$MISTREL_API_BASE|" .env
sed -i "s|GROUND_TRUTH_LLM_API_BASE=.*|GROUND_TRUTH_LLM_API_BASE=$GPT_LLM_API_BASE|" .env
sed -i "s|JUDGE_LLM_API_BASE=.*|JUDGE_LLM_API_BASE=$GPT_LLM_API_BASE|" .env
sed -i "s|CODE_LLM_API_BASE=.*|CODE_LLM_API_BASE=$GPT_LLM_API_BASE|" .env
```

### Set thinking

```bash
sed -i "s/GROUND_TRUTH_LLM_THINKING=.*/GROUND_TRUTH_LLM_THINKING=false/" .env
sed -i "s/JUDGE_LLM_THINKING=.*/JUDGE_LLM_THINKING=false/" .env
```

### Set tokens

```bash
MISTRAL_TOKEN=$(oc get secret default-token-e5-mistral-7b-instruct-sa -n model-e5-mistral-7b-instruct -o jsonpath='{.data.token}' | base64 --decode)

#GPT_LLM_TOKEN=$(oc get secret default-token-redhataigpt-oss-120b-sa -n model-gpt-oss-120b -o jsonpath='{.data.token}' | base64 --decode)

#GPT_LLM_TOKEN=""

sed -i "s/GRAPHRAG_LLM_TOKEN=.*/GRAPHRAG_LLM_TOKEN=$GPT_LLM_TOKEN/" .env
sed -i "s/EMBED_LLM_TOKEN=.*/EMBED_LLM_TOKEN=$MISTRAL_TOKEN/" .env
sed -i "s/GROUND_TRUTH_LLM_TOKEN=.*/GROUND_TRUTH_LLM_TOKEN=$GPT_LLM_TOKEN/" .env
sed -i "s/JUDGE_LLM_TOKEN=.*/JUDGE_LLM_TOKEN=$GPT_LLM_TOKEN/" .env
sed -i "s/CODE_LLM_TOKEN=.*/CODE_LLM_TOKEN=$GPT_LLM_TOKEN/" .env
```

### Set Kubeflow Pipelines

```bash
sed -i "s/KFP_NAMESPACE=.*/KFP_NAMESPACE=$DATA_SCIENCE_PROJECT_NAME/" .env
sed -i "s|KFP_DATA_GENERATION_OUTPUT_PATH=.*|KFP_DATA_GENERATION_OUTPUT_PATH=target|" .env
sed -i "s|KFP_DATA_INDEXING_OUTPUT_PATH=.*|KFP_DATA_INDEXING_OUTPUT_PATH=graphrag-source/output|" .env
sed -i "s|KFP_IMAGE_REGISTRY=.*|KFP_IMAGE_REGISTRY=quay.io/oawofolurh|" .env
sed -i "s|KFP_DATA_GENERATION_BASE_IMAGE_NAME=.*|KFP_DATA_GENERATION_BASE_IMAGE_NAME=agentic-wb|" .env
sed -i "s|KFP_DATA_GENERATION_BASE_IMAGE_VERSION=.*|KFP_DATA_GENERATION_BASE_IMAGE_VERSION=v8|" .env
sed -i "s|KFP_INDEXING_BASE_IMAGE_NAME=.*|KFP_INDEXING_BASE_IMAGE_NAME=graphrag-wb|" .env
sed -i "s|KFP_INDEXING_BASE_IMAGE_VERSION=.*|KFP_INDEXING_BASE_IMAGE_VERSION=v8|" .env
sed -i "s|KFP_ANALYSIS_BASE_IMAGE_NAME=.*|KFP_ANALYSIS_BASE_IMAGE_NAME=graphrag-wb|" .env
sed -i "s|KFP_ANALYSIS_BASE_IMAGE_VERSION=.*|KFP_ANALYSIS_BASE_IMAGE_VERSION=v8|" .env
```

### Set asset loader and custom evaluator

```bash
sed -i "s/ASSET_LOADER=.*/ASSET_LOADER=mlflow/" .env

sed -i "s/CUSTOM_EVALUATOR=.*/CUSTOM_EVALUATOR=mlflow/" .env
```

### Install and run

```bash
make install

# $ oc get mlflow -o yaml

make run-pipelines ARGS="--single-repo"
```
