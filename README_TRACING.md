# Distributed Tracing & LLM Observability Guide (`README_TRACING.md`)

AUGUR integrates **OpenTelemetry (OTel)** and **MLflow Tracing** (`MlFlowCustomTelemetry` in [`workflows/examples/code_understanding/telemetry/mlflow_custom_telemetry.py`](workflows/examples/code_understanding/telemetry/mlflow_custom_telemetry.py)) to automatically capture every LLM and embedding invocation across Kubeflow Pipeline runs, enrich spans with token usage and Kubeflow Pipeline metadata (`kfp.run_id`, `kfp.run_name`, `kfp.task_name`, `k8s.pod.name`), and dual-export traces to:

1. **Red Hat OpenShift Console (`Observe → Traces`)** backed by **Grafana Tempo (`TempoStack`)**
2. **MLflow UI (`Observability → Traces`)**

---

## 1. Required OpenShift Operators

Before running `make deploy-otel`, the following three operators must be installed and in the `Succeeded` phase in OpenShift **OperatorHub** (**Operators → OperatorHub**):

| Operator Name in OperatorHub | Package / CSV Prefix | Required CRD | Purpose |
| :--- | :--- | :--- | :--- |
| **Red Hat build of OpenTelemetry** | `opentelemetry-product` (`opentelemetry-operator`) | `opentelemetrycollectors.opentelemetry.io` | Manages the `OpenTelemetryCollector` that receives OTLP traces from pipeline pods and forwards them to Tempo. |
| **Tempo Operator** | `tempo-product` (`tempo-operator`) | `tempostacks.tempo.grafana.com` | Manages the multi-tenant `TempoStack` backend storing traces in MinIO S3 storage. |
| **Cluster Observability Operator** | `cluster-observability-operator` | `uiplugins.observability.openshift.io` | Provides the `UIPlugin` (`distributed-tracing`) that enables **Observe → Traces** in the OpenShift Web Console. |

### Operator Installation Tips
* **Installation Mode**: Choose **All namespaces on the cluster (default)** (which installs into `openshift-operators` or the operator's recommended cluster-wide namespace).
* **Troubleshooting `csv created in namespace with multiple operatorgroups, can't pick one automatically`**:
  If an operator fails with this error (typically in `openshift-operators-redhat`), check for duplicate `OperatorGroup` resources in that namespace:
  ```bash
  oc get operatorgroup -n openshift-operators-redhat
  ```
  Delete the extra `OperatorGroup` (or uninstall the operator from `openshift-operators-redhat` and reinstall it using the default cluster-wide installation mode).

---

## 2. Using `make deploy-otel` and What It Does

### Environment Configuration (`.env`)

Ensure the following variables are configured in your `.env` file (see [`.env.template`](.env.template)):

```dotenv
# Enable MLflow + OpenTelemetry tracing
CUSTOM_TELEMETRY=mlflow

# Target OpenTelemetry Collector & TempoStack settings
OTEL_SERVICE_NAME=code-understanding
OTEL_NAMESPACE=otel
OTEL_EXPORTER_OTLP_ENDPOINT=http://code-understanding-collector.otel.svc.cluster.local:4318
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://code-understanding-collector.otel.svc.cluster.local:4318/v1/traces
OTEL_EXPORTER=otlp_http
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf

# Dual-export traces to both OpenShift Tempo (OTLP) and the MLflow Tracking Server
MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT=true
OTEL_SEMCONV_STABILITY_OPT_IN=genai
```

### Running `make deploy-otel`

You can deploy the tracing stack standalone at any time:

```bash
make deploy-otel
```

*(Note: `make deploy` also invokes `deploy-otel` automatically when `OTEL_SERVICE_NAME` and `OTEL_NAMESPACE` are set in `.env`.)*

### What `make deploy-otel` Does Step-by-Step

Defined in [`Makefile`](Makefile) and [`resources/helm/templates/opentelemetry.yaml`](resources/helm/templates/opentelemetry.yaml), `make deploy-otel` performs the following actions:

1. **Verifies Required Operators**:
   Checks that the CRDs and `ClusterServiceVersion` (CSV) resources for `opentelemetry-operator`, `tempo-operator`, and `cluster-observability-operator` are installed and in the `Succeeded` phase. If any are missing, it exits with a clear error message.
2. **Creates Namespace & MinIO S3 Bucket (First Run)**:
   * Creates the target `OTEL_NAMESPACE` (e.g., `otel`).
   * Waits for the `minio` deployment in `KFP_NAMESPACE` to be available.
   * Runs a one-shot Kubernetes Job (`create-tempo-bucket` from [`resources/helm/templates/create-tempo-bucket-job.yaml`](resources/helm/templates/create-tempo-bucket-job.yaml)) to create the `tempo` S3 bucket in MinIO.
3. **Deploys Multi-Tenant `TempoStack` & RBAC**:
   * Creates `Secret/tempo-s3-secret` in `OTEL_NAMESPACE` pointing to MinIO.
   * Deploys `TempoStack` (`code-understanding`) configured with `tenants.mode: openshift`, tenant `dev`, and the Tempo `gateway` and `queryFrontend` enabled so the OpenShift Console can discover it.
   * Creates `ClusterRole` and `ClusterRoleBinding` resources granting the collector ServiceAccount write access to the `dev` tenant (`tempostack-traces-write-*`) and authenticated OpenShift Console users read access (`tempostack-traces-reader-*`).
4. **Deploys the `OpenTelemetryCollector`**:
   * Deploys `OpenTelemetryCollector` (`code-understanding`) in `OTEL_NAMESPACE`, listening on port `4317` (OTLP gRPC) and `4318` (OTLP HTTP), and forwarding traces with `X-Scope-OrgID: dev` and bearer token authentication to the `TempoStack` gateway.
5. **Enables the OpenShift Console `UIPlugin`**:
   * Applies [`resources/distributed-tracing-uiplugin.yaml`](resources/distributed-tracing-uiplugin.yaml) (`UIPlugin/distributed-tracing`) so **Observe → Traces** appears in the OpenShift Web Console.

---

## 3. Finding Traces by Run ID in OpenShift (`Observe → Traces`)

Every Kubeflow Pipeline task pod automatically tags its exported spans and resource attributes with:
* `kfp.run_id`: The Kubeflow Pipeline Run UUID
* `kfp.run_name`: The Kubeflow Pipeline Run name
* `kfp.task_name`: The pipeline step/task name
* `k8s.pod.name`: The Kubernetes pod name
* `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.total_tokens`: Extracted integer token counts
* Inline token breakdown in the span name: e.g., `AsyncCompletions (tokens: 1500 | in: 1200, out: 300)`

### Step 1: Find Your Kubeflow Pipeline Run ID

You can get the Run ID in any of three ways:

1. **From `make run-pipelines` output**:
   ```text
   Run details: https://ds-pipeline-dspa.codearcheology-dbrletic.svc.cluster.local:8443/#/runs/details/59a05dff-4f48-413c-a5e6-09ad49d86f0e
     Submitted run id: 59a05dff-4f48-413c-a5e6-09ad49d86f0e
   ```
2. **From running or completed pipeline pods via `oc`**:
   ```bash
   oc get pods -n <KFP_NAMESPACE> -l pipeline/runid -L pipeline/runid
   ```
3. **From the OpenShift AI Dashboard**:
   Navigate to **Data Science Pipelines → Runs**, click your run, and copy the **Run ID** from the URL or run details tab.

### Step 2: Query Traces in OpenShift Console (`Observe → Traces`)

1. In the OpenShift Web Console, navigate to **Observe → Traces**.
2. Set the dropdowns at the top:
   * **Tempo instance**: `<OTEL_NAMESPACE> / <OTEL_SERVICE_NAME>` (e.g., `otel / code-understanding`)
   * **Tenant**: `dev`
   * **Time range**: Ensure it covers when your pipeline ran (e.g., `Last 1 hour` or `Last 6 hours`).
   * **Limit**: Increase from `20` to `100` or `1000` to view all LLM calls from a run on one page.
3. Toggle on **Custom query** (TraceQL) and run:

   **All traces for a single Pipeline Run ID:**
   ```traceql
   { resource.service.name = "code-understanding" && .kfp.run_id = "<YOUR_RUN_ID>" }
   ```

   **All LLM calls with token usage for a single Pipeline Run ID:**
   ```traceql
   { resource.service.name = "code-understanding" && .kfp.run_id = "<YOUR_RUN_ID>" && span.mlflow.chat.tokenUsage != "" }
   ```

   **Filter LLM calls in a run that used more than 2,000 total tokens:**
   ```traceql
   { resource.service.name = "code-understanding" && .kfp.run_id = "<YOUR_RUN_ID>" && span.gen_ai.usage.total_tokens > 2000 }
   ```

### Viewing Token Counts and Total LLM Calls in `Observe → Traces`

* **Per-Call Token Breakdown**: Shown directly in the **Trace name** column of the results table:
  ```text
  code-understanding: AsyncCompletions (tokens: 1500 | in: 1200, out: 300)
  ```
  Clicking any trace row opens the waterfall view where `gen_ai.usage.total_tokens`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `kfp.run_id`, and `kfp.task_name` are listed under **Attributes**.
* **Total Number of LLM Calls in the UI**: Look at the pagination counter in the bottom-right corner of the results table (`1–20 of <TOTAL>`). Because each LLM call is exported as 1 trace, `<TOTAL>` is the number of LLM calls for that run (up to your selected **Limit**).
* **CLI One-Liner for Exact Total Calls & Summed Tokens from Tempo**:
  ```bash
  oc exec -n otel deploy/tempo-code-understanding-query-frontend -c tempo-query-frontend -- \
    wget -qO- --header="X-Scope-OrgID: 1610b0c3-c509-4592-a256-a1871353dbfa" \
    'http://localhost:3200/api/search?limit=5000&q=%7B%20resource.service.name%20%3D%20%22code-understanding%22%20%26%26%20.kfp.run_id%20%3D%20%22<YOUR_RUN_ID>%22%20%26%26%20span.mlflow.chat.tokenUsage%20!%3D%20%22%22%20%7D' \
    | python3 -c 'import sys, json, re; d = json.load(sys.stdin).get("traces", []); toks = [int(m.group(1)) for t in d for m in [re.search(r"tokens:\s*(\d+)", t.get("rootTraceName", ""))] if m]; print(f"Total LLM Calls: {len(d)}" + (f" | Total Tokens: {sum(toks)}" if toks else ""))'
  ```

---

## 4. Finding Traces by Run ID in MLflow (`Observability → Traces`)

With `MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT=true`, [`MlFlowCustomTelemetry`](workflows/examples/code_understanding/telemetry/mlflow_custom_telemetry.py) also tags every MLflow `TraceInfo` with `kfp.run_id`, `kfp.run_name`, `kfp.task_name`, and `k8s.pod.name`.

1. Open the **MLflow UI** and select your workspace and experiment (e.g., **`<KFP_NAMESPACE>/AIP-default`**).
2. In the left navigation sidebar under **Observability**, click **Traces**.
3. Set the **Time Range** dropdown at the top-left (e.g., `Last hour` or `Last 24 hours`).
4. Click the **Filters** button next to the search bar:
   * **Field**: Select `kfp.run_id`
   * **Operator**: Select `=`
   * **Value**: Paste your Kubeflow Pipeline Run ID (e.g., `43bde5c9-f151-4e73-ac7b-b8...`)
   * Click **Apply filters**.
5. The table will display all LLM traces for that pipeline run along with their **Request**, **Response**, **Tokens** badge, **Latency**, and total count in the top-right corner.
