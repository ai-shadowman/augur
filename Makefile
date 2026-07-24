ENV_FILE     ?= .env
GIT_REPO_URL := $(shell git remote get-url origin 2>/dev/null | sed 's|^git@\([^:]*\):\(.*\)$$|https://\1/\2|')

install:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Creating namespace $$KFP_NAMESPACE..." && \
	sed "s|{{ .Values.namespace }}|$$KFP_NAMESPACE|g; s|{{ .Values.requester }}|$$(oc whoami)|g" resources/helm/templates/namespace.yaml | oc apply -f - && \
	\
	echo "==> Granting mlflow role to default user for MLflow workspace access ..." && \
	oc adm policy add-role-to-user mlflow default -n $$KFP_NAMESPACE && \
	\
	echo "==> Running helm upgrade..." && \
	helm upgrade --install agent-mesh-for-sw resources/helm \
		--no-hooks \
		--create-namespace \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set minio.rootUser="$$AWS_ACCESS_KEY_ID" \
		--set minio.rootPassword="$$AWS_SECRET_ACCESS_KEY" \
		--set dataGeneration.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set dataGeneration.image.name="$$KFP_DATA_GENERATION_BASE_IMAGE_NAME" \
		--set dataGeneration.image.version="$$KFP_DATA_GENERATION_BASE_IMAGE_VERSION" \
		--set graphrag.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set graphrag.image.name="$$KFP_INDEXING_BASE_IMAGE_NAME" \
		--set graphrag.image.version="$$KFP_INDEXING_BASE_IMAGE_VERSION" \
		--set analysis.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set analysis.image.name="$$KFP_ANALYSIS_BASE_IMAGE_NAME" \
		--set analysis.image.version="$$KFP_ANALYSIS_BASE_IMAGE_VERSION"
	$(MAKE) apply-secrets
	@set -a && . $(ENV_FILE) && set +a && \
	[ "$$ASSET_LOADER" = "mlflow" ] && \
	echo "==> Preloading MLflow assets..." && \
	$(MAKE) preload-mlflow-assets || true && \
	$(MAKE) deploy-notebooks || true
	$(MAKE) upload-pipelines

deploy-notebooks:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Waiting for data-generation ImageStream to import..." && \
	until oc get imagestreamtag custom-data-generation:$$KFP_DATA_GENERATION_BASE_IMAGE_VERSION -n redhat-ods-applications -o jsonpath='{.image.dockerImageReference}' 2>/dev/null | grep -q '@sha256:'; do sleep 5; done && \
	DATAGEN_IMAGE="$$(oc get imagestream custom-data-generation -n redhat-ods-applications -o jsonpath='{.status.dockerImageRepository}'):$$KFP_DATA_GENERATION_BASE_IMAGE_VERSION" && \
	echo "  image: $$DATAGEN_IMAGE" && \
	\
	echo "==> Waiting for graphrag ImageStream to import..." && \
	until oc get imagestreamtag custom-graphrag:$$KFP_INDEXING_BASE_IMAGE_VERSION -n redhat-ods-applications -o jsonpath='{.image.dockerImageReference}' 2>/dev/null | grep -q '@sha256:'; do sleep 5; done && \
	GRAPHRAG_IMAGE="$$(oc get imagestream custom-graphrag -n redhat-ods-applications -o jsonpath='{.status.dockerImageRepository}'):$$KFP_INDEXING_BASE_IMAGE_VERSION" && \
	echo "  image: $$GRAPHRAG_IMAGE" && \
	\
	echo "==> Deploying notebooks..." && \
	sleep 10 && \
	oc patch notebook data-generation -n $$KFP_NAMESPACE -p '{"metadata":{"finalizers":null}}' --type=merge 2>/dev/null || true && \
	oc patch notebook graphrag-indexing -n $$KFP_NAMESPACE -p '{"metadata":{"finalizers":null}}' --type=merge 2>/dev/null || true && \
	oc delete notebook data-generation graphrag-indexing -n $$KFP_NAMESPACE --ignore-not-found=true && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set dataGeneration.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set dataGeneration.image.name="$$KFP_DATA_GENERATION_BASE_IMAGE_NAME" \
		--set dataGeneration.image.version="$$KFP_DATA_GENERATION_BASE_IMAGE_VERSION" \
		--set dataGeneration.image.digestRef="$$DATAGEN_IMAGE" \
		--set graphrag.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set graphrag.image.name="$$KFP_INDEXING_BASE_IMAGE_NAME" \
		--set graphrag.image.version="$$KFP_INDEXING_BASE_IMAGE_VERSION" \
		--set graphrag.image.digestRef="$$GRAPHRAG_IMAGE" \
		--set deployNotebooks=true \
		-s templates/workbench-notebooks.yaml | oc apply -f -

apply-secrets:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Applying git-credentials secret..." && \
	oc create secret generic git-credentials \
		--from-literal=GIT_USERNAME="$$GIT_USERNAME" \
		--from-literal=GIT_TOKEN="$$GIT_TOKEN" \
		-n $$KFP_NAMESPACE --dry-run=client -o yaml | oc apply -f - && \
	\
	echo "==> Recreating secret code-understanding-env..." && \
	oc delete secret code-understanding-env -n $$KFP_NAMESPACE --ignore-not-found=true && \
	oc create secret generic code-understanding-env --from-env-file $(ENV_FILE) -n $$KFP_NAMESPACE && \
	oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge \
		-p "{\"stringData\":{\"MLFLOW_NAMESPACE\":\"$$KFP_NAMESPACE\",\"MLFLOW_TRACKING_TOKEN\":\"$$(oc whoami --show-token)\"}}"

build-images:
	@set -a && . $(ENV_FILE) && set +a && \
	DATAGEN_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_DATA_GENERATION_BASE_IMAGE_NAME:$$KFP_DATA_GENERATION_BASE_IMAGE_VERSION" && \
	INDEX_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_INDEXING_BASE_IMAGE_NAME:$$KFP_INDEXING_BASE_IMAGE_VERSION" && \
	ANALYSIS_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_ANALYSIS_BASE_IMAGE_NAME:$$KFP_ANALYSIS_BASE_IMAGE_VERSION" && \
	\
	echo "==> Building data generation image..." && \
	podman build -t "$$DATAGEN_IMG" resources/images/data-generation && \
	echo "==> Pushing data generation image..." && \
	podman push "$$DATAGEN_IMG" && \
	\
	echo "==> Building indexing image..." && \
	podman build -t "$$INDEX_IMG" resources/images/data-indexing && \
	echo "==> Pushing indexing image..." && \
	podman push "$$INDEX_IMG" && \
	\
	echo "==> Building analysis image..." && \
	podman build -t "$$ANALYSIS_IMG" resources/images/data-generation && \
	echo "==> Pushing analysis image..." && \
	podman push "$$ANALYSIS_IMG"

upload-pipelines:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Waiting for pipeline server to be ready..." && \
	until oc get deployment ds-pipeline-dspa -n $$KFP_NAMESPACE 2>/dev/null; do sleep 5; done && \
	oc wait deployment/ds-pipeline-dspa -n $$KFP_NAMESPACE --for=condition=Available --timeout=300s && \
	\
	echo "==> Uploading Kubeflow pipelines..." && \
	oc delete job upload-kubeflow-pipelines -n $$KFP_NAMESPACE --ignore-not-found=true && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set repoUrl="$(GIT_REPO_URL)" \
		-s templates/upload-pipelines-job.yaml | oc apply -n $$KFP_NAMESPACE -f -

preload-mlflow-assets:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Deleting existing upload-assets job..." && \
	oc delete job upload-assets -n $$KFP_NAMESPACE --ignore-not-found=true && \
	\
	echo "==> Submitting upload-assets job..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set repoUrl="$(GIT_REPO_URL)" \
		-s templates/upload-assets-job.yaml | oc apply -n $$KFP_NAMESPACE -f -

run-adhoc-query:
	@[ -z "$(QUESTION)" ] && { echo "Error: QUESTION is required. Usage: make run-adhoc-query QUESTION=\"your question\"" >&2; exit 1; } || true
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Storing query parameters..." && \
	oc delete secret adhoc-query-params -n $$KFP_NAMESPACE --ignore-not-found=true && \
	oc create secret generic adhoc-query-params \
		--from-literal=QUESTION='$(QUESTION)' \
		-n $$KFP_NAMESPACE && \
	\
	echo "==> Submitting adhoc query job..." && \
	oc delete job run-adhoc-query -n $$KFP_NAMESPACE --ignore-not-found=true && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set adhocQuery.run=true \
		--set-string adhocQuery.graphragDir="$${GRAPHRAG_DIR:-graph_rag_app/source}" \
		--set-string adhocQuery.useGlobal="$${USE_GLOBAL:-1}" \
		--set-string adhocQuery.retryCount="$${RETRY_COUNT:-3}" \
		--set analysis.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set analysis.image.name="$$KFP_ANALYSIS_BASE_IMAGE_NAME" \
		--set analysis.image.version="$$KFP_ANALYSIS_BASE_IMAGE_VERSION" \
		-s templates/adhoc-query-job.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	\
	echo "==> Waiting for adhoc-query container to start..." && \
	until oc logs job/run-adhoc-query -n $$KFP_NAMESPACE >/dev/null 2>&1; do sleep 2; done && \
	\
	echo "==> Streaming query results..." && \
	oc logs -f job/run-adhoc-query -n $$KFP_NAMESPACE

run-pipelines:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Submitting run-pipelines job..." && \
	oc delete job run-pipelines -n $$KFP_NAMESPACE --ignore-not-found=true && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set runPipelines.run=true \
		--set-string runPipelines.args="$${ARGS:---single}" \
		--set-string runPipelines.targetPath="$${KFP_DATA_GENERATION_OUTPUT_PATH:-target}" \
		--set-string runPipelines.graphragSourcePath="$${KFP_DATA_INDEXING_OUTPUT_PATH:-graph_rag_app/source}" \
		-s templates/run-pipelines-job.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	\
	echo "==> Waiting for run-pipelines container to start..." && \
	until oc logs job/run-pipelines -n $$KFP_NAMESPACE >/dev/null 2>&1; do sleep 2; done && \
	\
	echo "==> Streaming pipeline run results..." && \
	oc logs -f job/run-pipelines -n $$KFP_NAMESPACE
