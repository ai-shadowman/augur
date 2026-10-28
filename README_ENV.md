| Variable                            | Example Value                                                                    | Req'ed?   | Description |
|-------------------------------------|----------------------------------------------------------------------------------|-----------|-------------|
| AUGUR_GIT_REPO_USERNAME             | kfrankli                                                                         | false     |             |
| AUGUR_GIT_REPO_TOKEN                | ghp_...                                                                          | false     |             |
| AWS_ACCESS_KEY_ID                   | minio                                                                            | true      |             |
| AWS_SECRET_ACCESS_KEY               | minio123                                                                         | true      |             |
| AWS_S3_BUCKET                       | data                                                                             | true      |             |
| MINIO_IMAGE                         | docker.io/pgsty/silo:latest                                                      | true      |             |
| MINIO_MC_IMAGE                      | docker.io/pgsty/mc:latest                                                        | true      |             |
| ENABLE_TLS_VERIFICATION             |                                       FALSE                                      | true      |             |
| GIT_USERNAME                        | kfrankli                                                                         | true      |             |
| GIT_TOKEN                           | ghp_...                                                                          | false     |             |
| GIT_REPO                            | https://github.com/kfrankli/northpolesouthern-monorepo-symlink.git               | true      |             |
| GIT_BRANCH                          | main                                                                             | true      |             |
| GIT_REPO_LIST                       | workflows/examples/code_understanding/assets/repos/repo_list1.json               | true      |             |
| GRAPHRAG_LLM_TOKEN                  | sk-or-v1-                                                                        | true      |             |
| GRAPHRAG_LLM_ID                     | openai/gpt-oss-120b                                                              | true      |             |
| GRAPHRAG_LLM_API_BASE               | https://openrouter.ai/api/v1/                                                    | true      |             |
| GRAPHRAG_LLM_PROVIDER               | openrouter                                                                       | true      |             |
| GRAPHRAG_LLM_PROVIDER_SETTINGS_XML  | openrouter                                                                       | true      |             |
| EMBED_LLM_TOKEN                     | base64                                                                           | true      |             |
| EMBED_LLM_API_BASE                  | https://mistral-predictor.model-e5-mistral-7b-instruct.svc.cluster.local:8443/v1 | true      |             |
| EMBED_LLM_ID                        | mistral                                                                          | true      |             |
| EMBED_LLM_PROVIDER                  | hosted_vllm                                                                      | true      |             |
| EMBED_LLM_PROVIDER_SETTINGS_XML     | openai                                                                           | true      |             |
| GROUND_TRUTH_LLM_TOKEN              | sk-or-v1                                                                         | true      |             |
| GROUND_TRUTH_LLM_ID                 | openai/gpt-oss-120b                                                              | true      |             |
| GROUND_TRUTH_LLM_API_BASE           | https://openrouter.ai/api/v1/                                                    | true      |             |
| GROUND_TRUTH_LLM_PROVIDER           | openrouter                                                                       | true      |             |
| GROUND_TRUTH_LLM_THINKING           |                                       FALSE                                      | true      |             |
| JUDGE_LLM_TOKEN                     | sk-or-v1-                                                                        | true      |             |
| JUDGE_LLM_ID                        | openai/gpt-oss-120b                                                              | true      |             |
| JUDGE_LLM_API_BASE                  | https://openrouter.ai/api/v1/                                                    | true      |             |
| JUDGE_LLM_PROVIDER                  | openrouter                                                                       | true      |             |
| JUDGE_LLM_THINKING                  |                                       FALSE                                      | true      |             |
| CODE_LLM_TOKEN                      | sk-or-v1-                                                                        | true      |             |
| CODE_LLM_API_BASE                   | https://openrouter.ai/api/v1/                                                    | true      |             |
| CODE_LLM_ID                         | openai/gpt-oss-120b                                                              | true      |             |
| CODE_LLM_PROVIDER                   | openrouter                                                                       | true      |             |
| KFP_NAMESPACE                       | codearcheology                                                                   | true      |             |
| KFP_DATA_GENERATION_OUTPUT_PATH     | target                                                                           | true      |             |
| KFP_DATA_INDEXING_OUTPUT_PATH       | graphrag-source/output                                                           | true      |             |
| KFP_IMAGE_REGISTRY                  | quay.io/ai-shadowman                                                             | true      |             |
| KFP_DATA_GENERATION_BASE_IMAGE_NAME | data-generation                                                                  | true      |             |
| KFP_DATA_GENERATION_BASE_IMAGE_TAG  | latest                                                                           | true      |             |
| KFP_INDEXING_BASE_IMAGE_NAME        | data-indexing                                                                    | true      |             |
| KFP_INDEXING_BASE_IMAGE_TAG         | latest                                                                           | true      |             |
| KFP_ANALYSIS_BASE_IMAGE_NAME        | data-analysis                                                                    | true      |             |
| KFP_ANALYSIS_BASE_IMAGE_TAG         | latest                                                                           | true      |             |
| KFP_PIPELINE_TOOLS_IMAGE_NAME       | pipeline-tools                                                                   | true      |             |
| KFP_PIPELINE_TOOLS_IMAGE_TAG        | latest                                                                           | true      |             |
| PIP_INDEX_URL                       | https://pypi.org/simple                                                          | true      |             |
| MLFLOW_TRACKING_URI                 | https://mlflow.redhat-ods-applications.svc.cluster.local:8443                    | true      |             |
| MLFLOW_TRACKING_INSECURE_TLS        |                                       TRUE                                       | true      |             |
| MLFLOW_TRACKING_AUTH                | kubernetes-namespaced                                                            | true      |             |
| ASSET_LOADER                        | mlflow                                                                           | true      |             |
| CUSTOM_EVALUATOR                    | mlflow                                                                           |           |             |
| LOGLEVEL                            | info                                                                             |           |             |
| PYTHONWARNINGS                      | ignore:Unverified                                                                |           |             |
| LITELLM_LOCAL_MODEL_COST_MAP        |                                       TRUE                                       |           |             |
| LITELLM_TELEMETRY                   |                                       FALSE                                      |           |             |
| DISABLE_LITELLM_VERSION_CHECK       |                                       TRUE                                       |           |             |