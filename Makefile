ENV_FILE                ?= ./.env
-include $(ENV_FILE)

ifndef AUGUR_GIT_REPO_URL
    AUGUR_GIT_REPO_URL  := $(shell git remote get-url origin 2>/dev/null | sed 's|^git@\([^:]*\):\(.*\)$$|https://\1/\2|')
endif
$(info ==> Using AUGUR_GIT_REPO_URL: $(AUGUR_GIT_REPO_URL))

ifndef AUGUR_GIT_REPO_BRANCH
    AUGUR_GIT_REPO_BRANCH := $(shell git branch --show-current 2>/dev/null)
endif
$(info ==> Using AUGUR_GIT_REPO_BRANCH: $(AUGUR_GIT_REPO_BRANCH))

ifndef CLUSTER_DOMAIN  
    CLUSTER_DOMAIN      := $(shell oc get ingress.config cluster -o jsonpath='{.spec.domain}' 2>/dev/null)
endif
$(info ==> Using CLUSTER_DOMAIN: $(CLUSTER_DOMAIN))

PIPELINE_GIT_REPO   	?=
PIPELINE_GIT_BRANCH 	?=
PIPELINE_GIT_REPO_LIST	?=
PIPELINE_RUN_ID     	?=
PROJECT_SLUG        	?=
PROJECT_WORKSPACE   	?=
CONSOLE_APP_IMAGE_NAME    ?= console-app
CONSOLE_PLUGIN_IMAGE_NAME ?= console-plugin
CONSOLE_IMAGE_TAG         ?= latest
CONSOLE_ROUTE_HOST        ?=

install:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Creating namespace $$KFP_NAMESPACE..." && \
	helm template agent-mesh-for-sw resources/helm \
             --set namespace="$$KFP_NAMESPACE" \
             --set requester="$$(oc whoami)" \
             -s templates/namespace.yaml | oc apply -f - && \
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
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set augurRepoUsername="$(AUGUR_GIT_REPO_USERNAME)" \
		--set augurRepoToken="$(AUGUR_GIT_REPO_TOKEN)" \
		--set minio.rootUser="$$AWS_ACCESS_KEY_ID" \
		--set minio.rootPassword="$$AWS_SECRET_ACCESS_KEY" \
		--set minio.image="$${MINIO_IMAGE:-docker.io/pgsty/silo:latest}" \
		--set minio.mcImage="$${MINIO_MC_IMAGE:-docker.io/pgsty/mc:latest}" \
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
		--set clusterDomain="$(CLUSTER_DOMAIN)" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}"
	$(MAKE) apply-secrets
	$(MAKE) deploy-otel
	@set -a && . $(ENV_FILE) && set +a && \
	if [ "$$ASSET_LOADER" = "mlflow" ]; then \
		echo "==> Preloading MLflow assets..." && \
		$(MAKE) upload-mlflow-assets; \
	fi
	$(MAKE) upload-pipelines

apply-secrets:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	echo "==> Recreating secret code-understanding-env..." && \
	oc delete secret code-understanding-env -n $$KFP_NAMESPACE --ignore-not-found=true && \
	oc create secret generic code-understanding-env \
		--from-env-file $(ENV_FILE) \
		-n $$KFP_NAMESPACE && \
	\
	oc set data secret/code-understanding-env \
		--from-literal=AUGUR_GIT_REPO_URL="$(AUGUR_GIT_REPO_URL)" \
		--from-literal=AUGUR_GIT_REPO_BRANCH="$(AUGUR_GIT_REPO_BRANCH)" \
		-n $$KFP_NAMESPACE && \
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

update-secrets:
	$(MAKE) apply-secrets

build-images:
	@set -a && . $(ENV_FILE) && set +a && \
	DATAGEN_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_DATA_GENERATION_BASE_IMAGE_NAME:$$KFP_DATA_GENERATION_BASE_IMAGE_TAG" && \
	INDEX_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_INDEXING_BASE_IMAGE_NAME:$$KFP_INDEXING_BASE_IMAGE_TAG" && \
	ANALYSIS_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_ANALYSIS_BASE_IMAGE_NAME:$$KFP_ANALYSIS_BASE_IMAGE_TAG" && \
	TOOLS_IMG="$$KFP_IMAGE_REGISTRY/$$KFP_PIPELINE_TOOLS_IMAGE_NAME:$$KFP_PIPELINE_TOOLS_IMAGE_TAG" && \
	\
	echo "==> Building data generation image..." && \
	podman build -f resources/images/data-generation/Containerfile \
		$${KFP_BASE_IMAGE:+--build-arg BASE_IMAGE=$$KFP_BASE_IMAGE} \
		-t "$$DATAGEN_IMG" . && \
	echo "==> Pushing data generation image..." && \
	podman push "$$DATAGEN_IMG" && \
	\
	echo "==> Building indexing image..." && \
	podman build -f resources/images/data-indexing/Containerfile \
		$${KFP_BASE_IMAGE:+--build-arg BASE_IMAGE=$$KFP_BASE_IMAGE} \
		-t "$$INDEX_IMG" . && \
	echo "==> Pushing indexing image..." && \
	podman push "$$INDEX_IMG" && \
	\
	echo "==> Building analysis image..." && \
	podman build -f resources/images/data-analysis/Containerfile \
		$${KFP_BASE_IMAGE:+--build-arg BASE_IMAGE=$$KFP_BASE_IMAGE} \
		-t "$$ANALYSIS_IMG" . && \
	echo "==> Pushing analysis image..." && \
	podman push "$$ANALYSIS_IMG" && \
	\
	echo "==> Building pipeline-tools image..." && \
	podman build -f resources/images/pipeline-tools/Containerfile \
		$${KFP_BASE_IMAGE:+--build-arg BASE_IMAGE=$$KFP_BASE_IMAGE} \
		-t "$$TOOLS_IMG" . && \
	echo "==> Pushing pipeline-tools image..." && \
	podman push "$$TOOLS_IMG"

build-console-app-image:
	@set -a && . $(ENV_FILE) && set +a && \
	CONSOLE_IMG="$$KFP_IMAGE_REGISTRY/$${CONSOLE_APP_IMAGE_NAME:-$(CONSOLE_APP_IMAGE_NAME)}:$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	echo "==> Building console application image: $$CONSOLE_IMG..." && \
	podman build -f ui/Dockerfile -t "$$CONSOLE_IMG" . && \
	echo "==> Pushing console application image: $$CONSOLE_IMG..." && \
	podman push "$$CONSOLE_IMG"

build-console-plugin-image:
	@set -a && . $(ENV_FILE) && set +a && \
	PLUGIN_IMG="$$KFP_IMAGE_REGISTRY/$${CONSOLE_PLUGIN_IMAGE_NAME:-$(CONSOLE_PLUGIN_IMAGE_NAME)}:$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	echo "==> Building console plugin image: $$PLUGIN_IMG..." && \
	podman build -f console-plugin/Dockerfile -t "$$PLUGIN_IMG" console-plugin

push-console-plugin-image:
	@set -a && . $(ENV_FILE) && set +a && \
	PLUGIN_IMG="$$KFP_IMAGE_REGISTRY/$${CONSOLE_PLUGIN_IMAGE_NAME:-$(CONSOLE_PLUGIN_IMAGE_NAME)}:$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	echo "==> Pushing console plugin image: $$PLUGIN_IMG..." && \
	podman push "$$PLUGIN_IMG"

build-push-all:
	$(MAKE) build-images
	$(MAKE) build-console-app-image
	$(MAKE) build-console-plugin-image
	$(MAKE) push-console-plugin-image

oc-build-images:
	@set -a && . $(ENV_FILE) && set +a && \
	echo "==> Kicking off OpenShift BuildConfigs in namespace $$KFP_NAMESPACE..." && \
	oc start-build "$${KFP_DATA_GENERATION_BASE_IMAGE_NAME:-data-generation}" -n "$$KFP_NAMESPACE" $${BUILD_FLAGS:-} && \
	oc start-build "$${KFP_INDEXING_BASE_IMAGE_NAME:-data-indexing}" -n "$$KFP_NAMESPACE" $${BUILD_FLAGS:-} && \
	oc start-build "$${KFP_ANALYSIS_BASE_IMAGE_NAME:-data-analysis}" -n "$$KFP_NAMESPACE" $${BUILD_FLAGS:-} && \
	oc start-build "$${KFP_PIPELINE_TOOLS_IMAGE_NAME:-pipeline-tools}" -n "$$KFP_NAMESPACE" $${BUILD_FLAGS:-}

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
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set augurRepoUsername="$(AUGUR_GIT_REPO_USERNAME)" \
		--set augurRepoToken="$(AUGUR_GIT_REPO_TOKEN)" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
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
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set augurRepoUsername="$(AUGUR_GIT_REPO_USERNAME)" \
		--set augurRepoToken="$(AUGUR_GIT_REPO_TOKEN)" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
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
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set augurRepoUsername="$(AUGUR_GIT_REPO_USERNAME)" \
		--set augurRepoToken="$(AUGUR_GIT_REPO_TOKEN)" \
		--set adhocQuery.run=true \
		--set-string adhocQuery.jobId="$$JOB_ID" \
		--set-string adhocQuery.useGlobal="$(if $(GIT_REPO),0,1)" \
		--set-string adhocQuery.gitRepo="$(GIT_REPO)" \
		--set-string adhocQuery.gitBranch="$(GIT_BRANCH)" \
		--set-string adhocQuery.retryCount="$${RETRY_COUNT:-3}" \
		--set-string adhocQuery.pipelineRunId="$(PIPELINE_RUN_ID)" \
		--set-string adhocQuery.projectSlug="$(PROJECT_SLUG)" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set analysis.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set analysis.image.name="$$KFP_ANALYSIS_BASE_IMAGE_NAME" \
		--set analysis.image.tag="$$KFP_ANALYSIS_BASE_IMAGE_TAG" \
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
	JOB_ID="$$(date +%Y%m%d%H%M%S)$$(printf '%04x' $$((RANDOM)))" && \
	\
	[ -n "$(PIPELINE_GIT_REPO)" ]   && oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge -p '{"stringData":{"GIT_REPO":"$(PIPELINE_GIT_REPO)"}}' || true && \
	[ -n "$(PIPELINE_GIT_BRANCH)" ] && oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge -p '{"stringData":{"GIT_BRANCH":"$(PIPELINE_GIT_BRANCH)"}}' || true && \
	[ -n "$(PIPELINE_RUN_ID)" ]     && oc patch secret code-understanding-env -n $$KFP_NAMESPACE \
		--type=merge -p '{"stringData":{"PIPELINE_RUN_ID":"$(PIPELINE_RUN_ID)"}}' || true && \
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
	echo "==> Submitting run-pipelines-$$JOB_ID job..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set augurRepoUsername="$(AUGUR_GIT_REPO_USERNAME)" \
		--set augurRepoToken="$(AUGUR_GIT_REPO_TOKEN)" \
		--set runPipelines.run=true \
		--set-string runPipelines.jobId="$$JOB_ID" \
		--set-string runPipelines.args="$${ARGS:---single-repo}" \
		--set-string runPipelines.targetPath="$${KFP_DATA_GENERATION_OUTPUT_PATH:-target}" \
		--set-string runPipelines.graphragSourcePath="$${KFP_DATA_INDEXING_OUTPUT_PATH:-graph_rag_app/source}" \
		--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
		--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
		--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
		--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}" \
		-s templates/run-pipelines-job.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	\
	echo "==> Waiting for run-pipelines-$$JOB_ID container to start..." && \
	until oc logs job/run-pipelines-$$JOB_ID -n $$KFP_NAMESPACE >/dev/null 2>&1; do sleep 2; done && \
	\
	echo "==> Streaming pipeline run results..." && \
	oc logs -f job/run-pipelines-$$JOB_ID -n $$KFP_NAMESPACE

benchmark-llm:
	@set -a && . $(ENV_FILE) && set +a && \
	if [ "$${LOCAL:-0}" = "1" ] || [ "$${LOCAL:-false}" = "true" ]; then \
		echo "==> Running GuideLLM concurrency benchmark locally..." && \
		bash workflows/examples/code_understanding/scripts/benchmark_llm.sh $${ARGS:-}; \
	else \
		echo "==> Submitting benchmark-llm job to OpenShift namespace $$KFP_NAMESPACE..." && \
		oc delete job benchmark-llm -n $$KFP_NAMESPACE --ignore-not-found=true && \
		oc delete configmap benchmark-llm-scripts -n $$KFP_NAMESPACE --ignore-not-found=true && \
		oc create configmap benchmark-llm-scripts \
			--from-file=benchmark_llm.sh=workflows/examples/code_understanding/scripts/benchmark_llm.sh \
			--from-file=llm_benchmark.py=workflows/examples/code_understanding/utils/llm_benchmark.py \
			-n $$KFP_NAMESPACE && \
		ESCAPED_ARGS="$$(printf '%s' "$${ARGS:-}" | sed 's/,/\\,/g')" && \
		ESCAPED_LEVELS="$$(printf '%s' "$${GUIDELLM_CONCURRENCY_LEVELS:-}" | sed 's/,/\\,/g')" && \
		helm template agent-mesh-for-sw resources/helm \
			--set namespace="$$KFP_NAMESPACE" \
			--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
			--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
			--set augurRepoUsername="$(AUGUR_GIT_REPO_USERNAME)" \
			--set augurRepoToken="$(AUGUR_GIT_REPO_TOKEN)" \
			--set benchmarkLlm.run=true \
			--set-string benchmarkLlm.args="$$ESCAPED_ARGS" \
			--set-string benchmarkLlm.concurrencyLevels="$$ESCAPED_LEVELS" \
			--set-string benchmarkLlm.maxSeconds="$${GUIDELLM_MAX_SECONDS:-}" \
			--set-string benchmarkLlm.maxRequests="$${GUIDELLM_MAX_REQUESTS:-}" \
			--set pipelineTools.image.registry="$$KFP_IMAGE_REGISTRY" \
			--set pipelineTools.image.name="$$KFP_PIPELINE_TOOLS_IMAGE_NAME" \
			--set pipelineTools.image.tag="$$KFP_PIPELINE_TOOLS_IMAGE_TAG" \
			--set tls.enableVerification="$${ENABLE_TLS_VERIFICATION:-false}" \
			-s templates/benchmark-llm-job.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
		echo "==> Waiting for benchmark-llm container to start..." && \
		until oc logs job/benchmark-llm -n $$KFP_NAMESPACE >/dev/null 2>&1; do sleep 2; done && \
		echo "==> Streaming GuideLLM benchmark results..." && \
		oc logs -f job/benchmark-llm -n $$KFP_NAMESPACE; \
		oc delete configmap benchmark-llm-scripts -n $$KFP_NAMESPACE --ignore-not-found=true; \
	fi

deploy-otel:
	@set -a && . $(ENV_FILE) && set +a && \
	\
	if [ -z "$$OTEL_SERVICE_NAME" ] || [ -z "$$OTEL_NAMESPACE" ]; then \
		echo "Skipping deploy-otel: OTEL_SERVICE_NAME or OTEL_NAMESPACE is not set in $(ENV_FILE)."; \
		exit 0; \
	fi && \
	\
	echo "==> Checking for required OpenShift operators (OpenTelemetry, Tempo, Cluster Observability)..." && \
	MISSING_OPERATORS=0 && \
	CSV_LIST="$$(oc get csv -A --no-headers 2>/dev/null || true)" && \
	if ! oc get crd opentelemetrycollectors.opentelemetry.io >/dev/null 2>&1 || \
	   ! printf '%s\n' "$$CSV_LIST" | grep -qE 'opentelemetry-operator\..*Succeeded'; then \
		echo "ERROR: Red Hat build of OpenTelemetry Operator (opentelemetry-product) is not installed or not ready."; \
		MISSING_OPERATORS=1; \
	fi && \
	if ! oc get crd tempostacks.tempo.grafana.com >/dev/null 2>&1 || \
	   ! printf '%s\n' "$$CSV_LIST" | grep -qE 'tempo-operator\..*Succeeded'; then \
		echo "ERROR: Tempo Operator (tempo-product) is not installed or not ready."; \
		MISSING_OPERATORS=1; \
	fi && \
	if ! oc get crd uiplugins.observability.openshift.io >/dev/null 2>&1 || \
	   ! printf '%s\n' "$$CSV_LIST" | grep -qE 'cluster-observability-operator\..*Succeeded'; then \
		echo "ERROR: Cluster Observability Operator (cluster-observability-operator) is not installed or not ready."; \
		MISSING_OPERATORS=1; \
	fi && \
	if [ "$$MISSING_OPERATORS" -ne 0 ]; then \
		echo "Error: Please install the missing operator(s) from OpenShift OperatorHub before running deploy-otel."; \
		exit 1; \
	fi && \
	\
	if ! oc get tempostack $$OTEL_SERVICE_NAME -n $$OTEL_NAMESPACE >/dev/null 2>&1; then \
		echo "==> Creating OTel namespace $$OTEL_NAMESPACE..." && \
		oc create namespace $$OTEL_NAMESPACE --dry-run=client -o yaml | oc apply -f - && \
		\
		echo "==> Waiting for MinIO to be ready..." && \
		oc wait deployment/minio -n $$KFP_NAMESPACE --for=condition=Available --timeout=120s && \
		\
		echo "==> Creating Tempo S3 bucket..." && \
		oc delete job create-tempo-bucket -n $$OTEL_NAMESPACE --ignore-not-found=true && \
		helm template agent-mesh-for-sw resources/helm \
			--set otel.namespace=$$OTEL_NAMESPACE \
			--set otel.createBucket=true \
			--set minio.endpoint=http://minio-service.$$KFP_NAMESPACE.svc.cluster.local:9000 \
			--set minio.rootUser=$$AWS_ACCESS_KEY_ID \
			--set minio.rootPassword=$$AWS_SECRET_ACCESS_KEY \
			--set minio.mcImage="$${MINIO_MC_IMAGE:-docker.io/pgsty/mc:latest}" \
			-s templates/create-tempo-bucket-job.yaml | oc apply -f - && \
		oc wait job/create-tempo-bucket -n $$OTEL_NAMESPACE --for=condition=complete --timeout=120s && \
		oc delete job create-tempo-bucket -n $$OTEL_NAMESPACE --ignore-not-found=true; \
	else \
		echo "==> TempoStack already exists in $$OTEL_NAMESPACE, skipping S3 bucket job and updating OTel manifests..."; \
	fi && \
	\
	echo "==> Deploying TempoStack and OpenTelemetry Collector..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set otel.namespace=$$OTEL_NAMESPACE \
		--set minio.endpoint=http://minio-service.$$KFP_NAMESPACE.svc.cluster.local:9000 \
		--set minio.rootUser=$$AWS_ACCESS_KEY_ID \
		--set minio.rootPassword=$$AWS_SECRET_ACCESS_KEY \
		--set otel.enabled=true \
		--set otel.name=$$OTEL_SERVICE_NAME \
		$${OTEL_TEMPO_RETENTION:+--set otel.tempo.retention=$$OTEL_TEMPO_RETENTION} \
		-s templates/opentelemetry.yaml | oc apply -f - && \
	echo "==> Enabling OpenShift Distributed Tracing UIPlugin..." && \
	oc apply -f resources/distributed-tracing-uiplugin.yaml

# ============================================================================
# Console application
# ============================================================================

apply-console-src:
	@set -a && . $(ENV_FILE) && set +a && \
	echo "==> Publishing job scripts ConfigMap..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set console.jobScripts.enabled=true \
		--set-file console.jobScripts.runPipelines=workflows/examples/code_understanding/scripts/run_pipelines.sh \
		--set-file console.jobScripts.mlflowAssetLoader=workflows/examples/code_understanding/loaders/mlflow_asset_loader.py \
		--set-file console.jobScripts.defaultAssetLoader=workflows/examples/code_understanding/loaders/default_asset_loader.py \
		-s templates/console-job-scripts.yaml | oc apply -n $$KFP_NAMESPACE -f -

run-console-app:
	@set -a && . $(ENV_FILE) && set +a && \
	: "$${KFP_NAMESPACE:?Set KFP_NAMESPACE in .env or pass KFP_NAMESPACE=<namespace> to make}" && \
	export KFP_NAMESPACE && \
	export AGENTMESH_REPO_URL="$(AUGUR_GIT_REPO_URL)" && \
	export AGENTMESH_REPO_REF="$(AUGUR_GIT_REPO_BRANCH)" && \
	export MLFLOW_WORKSPACE="$$KFP_NAMESPACE" && \
	if [ -z "$${MLFLOW_TRACKING_TOKEN:-}" ]; then export MLFLOW_TRACKING_TOKEN="$$(oc whoami --show-token)"; fi && \
	KFP_ROUTE_HOST="$$(oc get route ds-pipeline-dspa -n "$$KFP_NAMESPACE" -o jsonpath='{.spec.host}')" && \
	: "$${KFP_ROUTE_HOST:?Could not find the ds-pipeline-dspa route in namespace $$KFP_NAMESPACE}" && \
	export KFP_HOST="https://$$KFP_ROUTE_HOST" && \
	uv run --project ui --frozen uvicorn --app-dir ui main:app --host 127.0.0.1 --port 8080

deploy-console-app: apply-console-src
	@set -a && . $(ENV_FILE) && set +a && \
	[ -n "$$KFP_IMAGE_REGISTRY" ] || { echo "KFP_IMAGE_REGISTRY must be set" >&2; exit 1; } && \
	CONSOLE_IMG="$$KFP_IMAGE_REGISTRY/$${CONSOLE_APP_IMAGE_NAME:-$(CONSOLE_APP_IMAGE_NAME)}:$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	echo "==> Deploying Code Understanding console ($$CONSOLE_IMG)..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set repoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set repoRef="$(AUGUR_GIT_REPO_BRANCH)" \
		--set console.enabled=true \
		--set-string console.image="$$CONSOLE_IMG" \
		--set-string console.route.host="$(CONSOLE_ROUTE_HOST)" \
		-s templates/console-app.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	oc rollout restart deployment/code-understanding-console -n $$KFP_NAMESPACE && \
	oc rollout status deployment/code-understanding-console -n $$KFP_NAMESPACE --timeout=300s && \
	ROUTE_HOST="$$(oc get route code-understanding-console -n $$KFP_NAMESPACE -o jsonpath='{.spec.host}')" && \
	echo "" && \
	echo "==> Open the console in your browser:" && \
	echo "    https://$$ROUTE_HOST" && \
	echo "" && \
	echo "    Or run: make port-forward-console-app  then open http://localhost:8080" && \
	echo ""

port-forward-console-app:
	@set -a && . $(ENV_FILE) && set +a && \
	echo "==> Forwarding http://localhost:8080 -> code-understanding-console:8080" && \
	oc port-forward svc/code-understanding-console 8080:8080 -n $$KFP_NAMESPACE

oc-deploy-console-app: apply-console-src
	@set -a && . $(ENV_FILE) && set +a && \
	APP_NAME="$${CONSOLE_APP_IMAGE_NAME:-$(CONSOLE_APP_IMAGE_NAME)}" && \
	APP_TAG="$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	echo "==> Ensuring OpenShift binary BuildConfig $$APP_NAME exists in $$KFP_NAMESPACE..." && \
	oc get bc "$$APP_NAME" -n "$$KFP_NAMESPACE" >/dev/null 2>&1 || \
		oc new-build --name="$$APP_NAME" --binary --strategy=docker -n "$$KFP_NAMESPACE" && \
	oc patch bc "$$APP_NAME" -n "$$KFP_NAMESPACE" --type=merge \
		-p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"ui/Dockerfile"}}}}' && \
	echo "==> Building $$APP_NAME in-cluster from local directory..." && \
	oc start-build "$$APP_NAME" --from-dir=. --follow -n "$$KFP_NAMESPACE" && \
	CONSOLE_IMG="image-registry.openshift-image-registry.svc:5000/$$KFP_NAMESPACE/$$APP_NAME:$$APP_TAG" && \
	echo "==> Deploying Code Understanding console ($$CONSOLE_IMG)..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set requester="$$(oc whoami)" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set repoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set repoRef="$(AUGUR_GIT_REPO_BRANCH)" \
		--set console.enabled=true \
		--set-string console.image="$$CONSOLE_IMG" \
		--set-string console.route.host="$(CONSOLE_ROUTE_HOST)" \
		-s templates/console-app.yaml | oc apply -n $$KFP_NAMESPACE -f - && \
	oc rollout restart deployment/code-understanding-console -n $$KFP_NAMESPACE && \
	oc rollout status deployment/code-understanding-console -n $$KFP_NAMESPACE --timeout=300s && \
	ROUTE_HOST="$$(oc get route code-understanding-console -n $$KFP_NAMESPACE -o jsonpath='{.spec.host}')" && \
	echo "" && \
	echo "==> Open the console in your browser:" && \
	echo "    https://$$ROUTE_HOST" && \
	echo ""

# ============================================================================
# OpenShift console plugin
# ============================================================================

apply-plugin-src: apply-console-src

deploy-console-plugin: apply-plugin-src
	@set -a && . $(ENV_FILE) && set +a && \
	[ -n "$$KFP_IMAGE_REGISTRY" ] || { echo "KFP_IMAGE_REGISTRY must be set" >&2; exit 1; } && \
	CONSOLE_IMG="$$KFP_IMAGE_REGISTRY/$${CONSOLE_APP_IMAGE_NAME:-$(CONSOLE_APP_IMAGE_NAME)}:$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	PLUGIN_IMG="$$KFP_IMAGE_REGISTRY/$${CONSOLE_PLUGIN_IMAGE_NAME:-$(CONSOLE_PLUGIN_IMAGE_NAME)}:$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	CONSOLE_HOST="$$(oc get route console -n openshift-console -o jsonpath='{.spec.host}')" && \
	CLUSTER_DOMAIN="$$(oc get ingress.config cluster -o jsonpath='{.spec.domain}')" && \
	echo "==> Deploying OpenShift console plugin and FastAPI backend..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set repoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set repoRef="$(AUGUR_GIT_REPO_BRANCH)" \
		--set clusterDomain="$$CLUSTER_DOMAIN" \
		--set consolePlugin.enabled=true \
		--set-string consolePlugin.image="$$PLUGIN_IMG" \
		--set-string consolePlugin.apiImage="$$CONSOLE_IMG" \
		--set consolePlugin.consoleBaseUrl="https://$$CONSOLE_HOST" \
		-s templates/console-plugin.yaml | oc apply -f - && \
	echo "==> Waiting for plugin-api Route hostname to be assigned..." && \
	oc wait route/code-understanding-plugin-api -n "$$KFP_NAMESPACE" \
		--for=jsonpath='{.spec.host}' --timeout=300s && \
	API_HOST="$$(oc get route code-understanding-plugin-api -n $$KFP_NAMESPACE -o jsonpath='{.spec.host}')" && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set repoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set repoRef="$(AUGUR_GIT_REPO_BRANCH)" \
		--set clusterDomain="$$CLUSTER_DOMAIN" \
		--set consolePlugin.enabled=true \
		--set-string consolePlugin.image="$$PLUGIN_IMG" \
		--set-string consolePlugin.apiImage="$$CONSOLE_IMG" \
		--set consolePlugin.consoleBaseUrl="https://$$CONSOLE_HOST" \
		--set consolePlugin.apiRouteHost="$$API_HOST" \
		-s templates/console-plugin.yaml | oc apply -f - && \
	oc rollout restart deployment/code-understanding-console-plugin -n $$KFP_NAMESPACE && \
	oc rollout restart deployment/code-understanding-plugin-api -n $$KFP_NAMESPACE && \
	oc rollout status deployment/code-understanding-console-plugin -n $$KFP_NAMESPACE --timeout=300s && \
	oc rollout status deployment/code-understanding-plugin-api -n $$KFP_NAMESPACE --timeout=300s && \
	$(MAKE) enable-console-plugin

oc-deploy-console-plugin: apply-plugin-src
	@set -a && . $(ENV_FILE) && set +a && \
	APP_NAME="$${CONSOLE_APP_IMAGE_NAME:-$(CONSOLE_APP_IMAGE_NAME)}" && \
	PLUGIN_NAME="$${CONSOLE_PLUGIN_IMAGE_NAME:-$(CONSOLE_PLUGIN_IMAGE_NAME)}" && \
	APP_TAG="$${CONSOLE_IMAGE_TAG:-$(CONSOLE_IMAGE_TAG)}" && \
	echo "==> Ensuring OpenShift binary BuildConfig $$APP_NAME exists in $$KFP_NAMESPACE..." && \
	oc get bc "$$APP_NAME" -n "$$KFP_NAMESPACE" >/dev/null 2>&1 || \
		oc new-build --name="$$APP_NAME" --binary --strategy=docker -n "$$KFP_NAMESPACE" && \
	oc patch bc "$$APP_NAME" -n "$$KFP_NAMESPACE" --type=merge \
		-p '{"spec":{"strategy":{"dockerStrategy":{"dockerfilePath":"ui/Dockerfile"}}}}' && \
	echo "==> Building $$APP_NAME in-cluster from local directory..." && \
	oc start-build "$$APP_NAME" --from-dir=. --follow -n "$$KFP_NAMESPACE" && \
	echo "==> Ensuring OpenShift binary BuildConfig $$PLUGIN_NAME exists in $$KFP_NAMESPACE..." && \
	oc get bc "$$PLUGIN_NAME" -n "$$KFP_NAMESPACE" >/dev/null 2>&1 || \
		oc new-build --name="$$PLUGIN_NAME" --binary --strategy=docker -n "$$KFP_NAMESPACE" && \
	echo "==> Building $$PLUGIN_NAME in-cluster from console-plugin/..." && \
	oc start-build "$$PLUGIN_NAME" --from-dir=console-plugin --follow -n "$$KFP_NAMESPACE" && \
	CONSOLE_IMG="image-registry.openshift-image-registry.svc:5000/$$KFP_NAMESPACE/$$APP_NAME:$$APP_TAG" && \
	PLUGIN_IMG="image-registry.openshift-image-registry.svc:5000/$$KFP_NAMESPACE/$$PLUGIN_NAME:$$APP_TAG" && \
	CONSOLE_HOST="$$(oc get route console -n openshift-console -o jsonpath='{.spec.host}')" && \
	CLUSTER_DOMAIN="$$(oc get ingress.config cluster -o jsonpath='{.spec.domain}')" && \
	echo "==> Deploying OpenShift console plugin ($$PLUGIN_IMG) and FastAPI backend ($$CONSOLE_IMG)..." && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set repoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set repoRef="$(AUGUR_GIT_REPO_BRANCH)" \
		--set clusterDomain="$$CLUSTER_DOMAIN" \
		--set consolePlugin.enabled=true \
		--set-string consolePlugin.image="$$PLUGIN_IMG" \
		--set-string consolePlugin.apiImage="$$CONSOLE_IMG" \
		--set consolePlugin.consoleBaseUrl="https://$$CONSOLE_HOST" \
		-s templates/console-plugin.yaml | oc apply -f - && \
	echo "==> Waiting for plugin-api Route hostname to be assigned..." && \
	oc wait route/code-understanding-plugin-api -n "$$KFP_NAMESPACE" \
		--for=jsonpath='{.spec.host}' --timeout=300s && \
	API_HOST="$$(oc get route code-understanding-plugin-api -n $$KFP_NAMESPACE -o jsonpath='{.spec.host}')" && \
	helm template agent-mesh-for-sw resources/helm \
		--set namespace="$$KFP_NAMESPACE" \
		--set augurRepoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set augurRepoBranch="$(AUGUR_GIT_REPO_BRANCH)" \
		--set repoUrl="$(AUGUR_GIT_REPO_URL)" \
		--set repoRef="$(AUGUR_GIT_REPO_BRANCH)" \
		--set clusterDomain="$$CLUSTER_DOMAIN" \
		--set consolePlugin.enabled=true \
		--set-string consolePlugin.image="$$PLUGIN_IMG" \
		--set-string consolePlugin.apiImage="$$CONSOLE_IMG" \
		--set consolePlugin.consoleBaseUrl="https://$$CONSOLE_HOST" \
		--set consolePlugin.apiRouteHost="$$API_HOST" \
		-s templates/console-plugin.yaml | oc apply -f - && \
	oc rollout restart deployment/code-understanding-console-plugin -n $$KFP_NAMESPACE && \
	oc rollout restart deployment/code-understanding-plugin-api -n $$KFP_NAMESPACE && \
	oc rollout status deployment/code-understanding-console-plugin -n $$KFP_NAMESPACE --timeout=300s && \
	oc rollout status deployment/code-understanding-plugin-api -n $$KFP_NAMESPACE --timeout=300s && \
	$(MAKE) enable-console-plugin

enable-console-plugin:
	@echo "==> Enabling code-understanding-console in OpenShift console..." && \
	EXISTING="$$(oc get consoles.operator.openshift.io cluster -o jsonpath='{.spec.plugins}' 2>/dev/null)" && \
	if echo "$$EXISTING" | grep -q 'code-understanding-console'; then \
	  echo "Plugin already enabled."; \
	else \
	  CONSOLE_GENERATION="$$(oc patch consoles.operator.openshift.io cluster --type=json \
	    -p='[{"op":"add","path":"/spec/plugins/-","value":"code-understanding-console"}]' \
	    -o jsonpath='{.metadata.generation}')" && \
	  echo "==> Waiting for the OpenShift console to load the plugin..." && \
	  oc wait consoles.operator.openshift.io/cluster \
	    --for=jsonpath='{.status.observedGeneration}'="$$CONSOLE_GENERATION" \
	    --timeout=300s; \
	fi && \
	oc rollout status deployment/console -n openshift-console --timeout=300s && \
	CONSOLE_HOST="$$(oc get route console -n openshift-console -o jsonpath='{.spec.host}')" && \
	echo "" && \
	echo "==> Open in OpenShift console:" && \
	echo "    https://$$CONSOLE_HOST/code-understanding" && \
	echo "    Application launcher: Code Understanding" && \
	echo ""
