
ENV_FILE            	?= ./.env
GIT_REPO_URL        	:= $(shell git remote get-url origin 2>/dev/null | sed 's|^git@\([^:]*\):\(.*\)$$|https://\1/\2|')
GIT_REPO_BRANCH     	:= $(shell git branch --show-current 2>/dev/null)
CLUSTER_DOMAIN      	:= $(shell oc get ingress.config cluster -o jsonpath='{.spec.domain}' 2>/dev/null)
PIPELINE_GIT_REPO   	?=
PIPELINE_GIT_BRANCH 	?=
PIPELINE_GIT_REPO_LIST	?=

install:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Creating namespace $$KFP_NAMESPACE..." && \
	sed "s|{{ .Values.namespace }}|$$KFP_NAMESPACE|g; s|{{ .Values.requester }}|$$(oc whoami)|g" resources/helm/templates/namespace.yaml | oc apply -f - && \
	\
	echo "==> Waiting for OpenShift to inject service CA into odh-trusted-ca-bundle..." && \
	until oc get configmap odh-trusted-ca-bundle -n $$KFP_NAMESPACE \
		-o jsonpath='{.data.ca-bundle\.crt}' 2>/dev/null | grep -q CERTIFICATE; do sleep 5; done && \
	\
	echo "==> Running helm upgrade..." && \
	helm upgrade --install agent-mesh-for-sw resources/helm \
		--no-hooks \
		--create-namespace \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set repoRef="$(GIT_REPO_BRANCH)" \
		--set minio.rootUser="$$AWS_ACCESS_KEY_ID" \
		--set minio.rootPassword="$$AWS_SECRET_ACCESS_KEY" \
		--set minio.image="$$MINIO_IMAGE" \
		--set dataGeneration.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set dataGeneration.image.name="$$KFP_DATA_GENERATION_BASE_IMAGE_NAME" \
		--set dataGeneration.image.tag="$$KFP_DATA_GENERATION_BASE_IMAGE_TAG" \
		--set graphrag.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set graphrag.image.name="$$KFP_INDEXING_BASE_IMAGE_NAME" \
		--set graphrag.image.tag="$$KFP_INDEXING_BASE_IMAGE_TAG" \
		--set analysis.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set analysis.image.name="$$KFP_ANALYSIS_BASE_IMAGE_NAME" \
		--set analysis.image.tag="$$KFP_ANALYSIS_BASE_IMAGE_TAG" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set pypiMirror="$$PYPI_MIRROR" \
		--set clusterDomain="$(CLUSTER_DOMAIN)" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}"
	$(MAKE) apply-secrets
	@set -a && . $(ENV_FILE) && set +a && \
	if [ "$$ASSET_LOADER" = "mlflow" ]; then \
		echo "==> Preloading MLflow assets..." && \
		$(MAKE) upload-mlflow-assets; \
	fi
	$(MAKE) upload-pipelines
	$(MAKE) deploy-notebooks

deploy-notebooks:
	@set -a && . $(ENV_FILE) && set +a && \
	if oc get notebook data-generation graphrag-indexing -n $$KFP_NAMESPACE 2>/dev/null | grep -q notebook; then \
		echo "==> Notebooks already exist, skipping deployment."; \
	else \
		echo "==> Waiting for data-generation ImageStream to import..." && \
		until oc get imagestreamtag custom-data-generation:$$KFP_DATA_GENERATION_BASE_IMAGE_TAG -n redhat-ods-applications -o jsonpath='{.image.dockerImageReference}' 2>/dev/null | grep -q '@sha256:'; do sleep 5; done && \
		DATAGEN_IMAGE="$$(oc get imagestream custom-data-generation -n redhat-ods-applications -o jsonpath='{.status.dockerImageRepository}'):$$KFP_DATA_GENERATION_BASE_IMAGE_TAG" && \
		echo "  image: $$DATAGEN_IMAGE" && \
		\
		echo "==> Waiting for graphrag ImageStream to import..." && \
		until oc get imagestreamtag custom-graphrag:$$KFP_INDEXING_BASE_IMAGE_TAG -n redhat-ods-applications -o jsonpath='{.image.dockerImageReference}' 2>/dev/null | grep -q '@sha256:'; do sleep 5; done && \
		GRAPHRAG_IMAGE="$$(oc get imagestream custom-graphrag -n redhat-ods-applications -o jsonpath='{.status.dockerImageRepository}'):$$KFP_INDEXING_BASE_IMAGE_TAG" && \
		echo "  image: $$GRAPHRAG_IMAGE" && \
		\
		echo "==> Waiting for analysis ImageStream to import..." && \
		until oc get imagestreamtag custom-graphrag:$$KFP_ANALYSIS_BASE_IMAGE_TAG -n redhat-ods-applications -o jsonpath='{.image.dockerImageReference}' 2>/dev/null | grep -q '@sha256:'; do sleep 5; done && \
		ANALYSIS_IMAGE="$$(oc get imagestream custom-graphrag -n redhat-ods-applications -o jsonpath='{.status.dockerImageRepository}'):$$KFP_ANALYSIS_BASE_IMAGE_TAG" && \
		echo "  image: $$ANALYSIS_IMAGE" && \
		\
		echo "==> Waiting for DSPA to be fully reconciled..." && \
		until oc get datasciencepipelinesapplication dspa -n $$KFP_NAMESPACE \
			-o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q "True"; do sleep 5; done && \
		\
		echo "==> Deploying notebooks..." && \
		helm template agent-mesh-for-sw resources/helm \
			--set namespace="$$KFP_NAMESPACE" \
			--set requester="$$(oc whoami)" \
			--set repoUrl="$(GIT_REPO_URL)" \
			--set repoRef="$(GIT_REPO_BRANCH)" \
			--set dataGeneration.image.registry="$$KFP_IMAGE_REGISTRY" \
			--set dataGeneration.image.name="$$KFP_DATA_GENERATION_BASE_IMAGE_NAME" \
			--set dataGeneration.image.tag="$$KFP_DATA_GENERATION_BASE_IMAGE_TAG" \
			--set dataGeneration.image.digestRef="$$DATAGEN_IMAGE" \
			--set graphrag.image.registry="$$KFP_IMAGE_REGISTRY" \
			--set graphrag.image.name="$$KFP_INDEXING_BASE_IMAGE_NAME" \
			--set graphrag.image.tag="$$KFP_INDEXING_BASE_IMAGE_TAG" \
			--set graphrag.image.digestRef="$$GRAPHRAG_IMAGE" \
			--set analysis.image.registry="$$KFP_IMAGE_REGISTRY" \
			--set analysis.image.name="$$KFP_ANALYSIS_BASE_IMAGE_NAME" \
			--set analysis.image.tag="$$KFP_ANALYSIS_BASE_IMAGE_TAG" \
			--set pypiMirror="$$PYPI_MIRROR" \
			--set analysis.image.digestRef="$$ANALYSIS_IMAGE" \
			--set deployNotebooks=true \
			-s templates/workbench-notebooks.yaml | oc apply -f -; \
	fi

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
	\
	REPO_LIST="$$GIT_REPO_LIST" && \
	if [ -n "$$PIPELINE_GIT_REPO_LIST" ] && [ -f "$$PIPELINE_GIT_REPO_LIST" ]; then \
		echo "==> PIPELINE_GIT_REPO_LIST is set, using instead of GIT_REPO_LIST"; \
		REPO_LIST="$$PIPELINE_GIT_REPO_LIST"; \
	fi && \
	if [ -n "$$REPO_LIST" ] && [ -f "$$REPO_LIST" ]; then \
		oc set data secret/code-understanding-env -n $$KFP_NAMESPACE \
			--from-file=GIT_REPO_LIST_CONTENTS="$$REPO_LIST"; \
	fi || true && \
	oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge \
		-p "{\"stringData\":{\"MLFLOW_WORKSPACE\":\"$$KFP_NAMESPACE\"}}"

build-images:
	@set -a && . $(ENV_FILE) && set +a && \
	DATAGEN_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_DATA_GENERATION_BASE_IMAGE_NAME:$$KFP_DATA_GENERATION_BASE_IMAGE_TAG" && \
	INDEX_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_INDEXING_BASE_IMAGE_NAME:$$KFP_INDEXING_BASE_IMAGE_TAG" && \
	ANALYSIS_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_ANALYSIS_BASE_IMAGE_NAME:$$KFP_ANALYSIS_BASE_IMAGE_TAG" && \
	TOOLS_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_PIPELINE_TOOLS_IMAGE_NAME:$$KFP_PIPELINE_TOOLS_IMAGE_TAG" && \
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
	podman build -t "$$ANALYSIS_IMG" resources/images/data-indexing && \
	echo "==> Pushing analysis image..." && \
	podman push "$$ANALYSIS_IMG" && \
	\
	echo "==> Building pipeline-tools image..." && \
	podman build -f resources/images/pipeline-tools/Containerfile \
		$${KFP_BASE_IMAGE:+--build-arg BASE_IMAGE=$$KFP_BASE_IMAGE} \
		-t "$$TOOLS_IMG" . && \
	echo "==> Pushing pipeline-tools image..." && \
	podman push "$$TOOLS_IMG"

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
		--set repoRef="$(GIT_REPO_BRANCH)" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set pypiMirror="$$PYPI_MIRROR" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}" \
		-s templates/upload-pipelines-job.yaml | oc apply -n $$KFP_NAMESPACE -f -

upload-mlflow-assets:
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
		--set repoRef="$(GIT_REPO_BRANCH)" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set pypiMirror="$$PYPI_MIRROR" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}" \
		-s templates/upload-assets-job.yaml | oc apply -n $$KFP_NAMESPACE -f -

run-adhoc-query:
	@[ -z "$(QUESTION_FILE)" ] && { echo "Error: QUESTION_FILE is required: generate it via wrappers/adhoc.sh." >&2; exit 1; } || true
	@set -a && . $(ENV_FILE) && set +a && \
	[ "$${ENABLE_TLS_VERIFICATION:-false}" = "false" ] || \
		oc get configmap odh-trusted-ca-bundle -n $$KFP_NAMESPACE >/dev/null 2>&1 || \
		{ echo "Error: TLS settings issue. Contact your system administrator." >&2; exit 1; } && \
	JOB_ID="$$(date +%Y%m%d%H%M%S)$$(printf '%04x' $$((RANDOM)))" && \
	\
	echo "==> Storing query parameters (job: $$JOB_ID)..." && \
	oc create configmap adhoc-query-$$JOB_ID \
		--from-file=question=$(QUESTION_FILE) \
		-n $$KFP_NAMESPACE && \
	\
	echo "==> Submitting adhoc query job..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set repoRef="$(GIT_REPO_BRANCH)" \
		--set adhocQuery.run=true \
		--set-string adhocQuery.jobId="$$JOB_ID" \
		--set-string adhocQuery.useGlobal="$(if $(GIT_REPO),0,1)" \
		--set-string adhocQuery.gitRepo="$(GIT_REPO)" \
		--set-string adhocQuery.gitBranch="$(GIT_BRANCH)" \
		--set-string adhocQuery.retryCount="$${RETRY_COUNT:-3}" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set analysis.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set analysis.image.name="$$KFP_ANALYSIS_BASE_IMAGE_NAME" \
		--set analysis.image.tag="$$KFP_ANALYSIS_BASE_IMAGE_TAG" \
		--set pypiMirror="$$PYPI_MIRROR" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}" \
		-s templates/run-adhoc-query-job.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	\
	echo "==> Waiting for query to complete..." && \
	oc wait job/run-adhoc-query-$$JOB_ID --for=condition=complete --timeout=1800s -n $$KFP_NAMESPACE; LOG_EXIT=$$?; \
	oc logs job/run-adhoc-query-$$JOB_ID -n $$KFP_NAMESPACE; \
	oc delete configmap adhoc-query-$$JOB_ID -n $$KFP_NAMESPACE --ignore-not-found=true; \
	exit $$LOG_EXIT

run-pipelines:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	[ -n "$(PIPELINE_GIT_REPO)" ]   && oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge -p '{"stringData":{"GIT_REPO":"$(PIPELINE_GIT_REPO)"}}' || true && \
	[ -n "$(PIPELINE_GIT_BRANCH)" ] && oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge -p '{"stringData":{"GIT_BRANCH":"$(PIPELINE_GIT_BRANCH)"}}' || true && \
	REPO_LIST="$$GIT_REPO_LIST" && \
	if [ -n "$$PIPELINE_GIT_REPO_LIST" ] && [ -f "$$PIPELINE_GIT_REPO_LIST" ]; then \
		echo "==> PIPELINE_GIT_REPO_LIST is set, using instead of GIT_REPO_LIST"; \
		REPO_LIST="$$PIPELINE_GIT_REPO_LIST"; \
	fi && \
	if [ -n "$$REPO_LIST" ] && [ -f "$$REPO_LIST" ]; then \
		echo "$$REPO_LIST"; \
		oc set data secret/code-understanding-env -n $$KFP_NAMESPACE \
			--from-file=GIT_REPO_LIST_CONTENTS="$$REPO_LIST"; \
		echo "$$REPO_LIST"; \
	fi || true && \
	echo "==> Submitting run-pipelines job..." && \
	oc delete job run-pipelines -n $$KFP_NAMESPACE --ignore-not-found=true && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set repoUrl="$(GIT_REPO_URL)" \
		--set repoRef="$(GIT_REPO_BRANCH)" \
		--set runPipelines.run=true \
		--set-string runPipelines.args="$${ARGS:---single-repo}" \
		--set-string runPipelines.targetPath="$${KFP_DATA_GENERATION_OUTPUT_PATH:-target}" \
		--set-string runPipelines.graphragSourcePath="$${KFP_DATA_INDEXING_OUTPUT_PATH:-graph_rag_app/source}" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}" \
		--set pypiMirror="$$PYPI_MIRROR" \
		-s templates/run-pipelines-job.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	\
	echo "==> Waiting for run-pipelines container to start..." && \
	until oc logs job/run-pipelines -n $$KFP_NAMESPACE >/dev/null 2>&1; do sleep 2; done && \
	\
	echo "==> Streaming pipeline run results..." && \
	oc logs -f job/run-pipelines -n $$KFP_NAMESPACE
