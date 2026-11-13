# Code Understanding Console UI Guide (`README_CONSOLE.md`)

AUGUR includes a web-based **Code Understanding Console** for running codebase analyses, monitoring Kubeflow Pipeline (KFP) runs in real time, inspecting migration and evaluation reports, and chatting with indexed codebases through GraphRAG — either directly inside the **Red Hat OpenShift Web Console** or as a standalone web app.

---

## 1. What the Console App Is

The console consists of two components that work together:

| Component | Location | Runtime | Role |
| :--- | :--- | :--- | :--- |
| **Console App & API** | [`ui/`](ui/), [`api/`](api/) | Python 3.11 / FastAPI (`uvicorn`) | Serves the web UI ([`ui/static/index.html`](ui/static/index.html)) and `/api/*` endpoints that talk to KFP, MLflow, GraphRAG, and the OpenShift API. |
| **OpenShift Console Plugin** | [`console-plugin/`](console-plugin/) | React / PatternFly / Nginx (`9443`) | Dynamic plugin (`code-understanding-console`) that adds **Code Understanding** to the OpenShift Web Console navigation and application launcher, proxying requests with the logged-in user's OpenShift token. |

### Key Capabilities

* **Repository Catalog Management**: View configured Git repositories and branches, see which repositories already have a GraphRAG index in MLflow, and add custom repositories/branches to the catalog.
* **Single-Repo & Multi-Repo Pipeline Execution**: Select **1** repository to run `single_repo_pipeline`, or **2+** repositories (with an optional project slug) to run cross-repository comparative analysis via `multi_repo_pipeline`.
* **Live Pipeline Stage Tracking**: Automatically polls active (`Pending` / `Running`) KFP runs and displays the current stage (`Data Generation (LLM Benchmark)`, `Data Generation (Cloning Repo)`, `Data Generation`, `GraphRAG Indexing`, `Evaluation`, `Analysis`) directly under the repository URL.
* **Migration & Evaluation Report Viewer**:
  * **Analysis tab**: Renders Markdown migration reports (`migration_report_*.md`) with embedded Mermaid architecture/dependency diagrams and one-click Markdown download.
  * **Analysis Evaluation tab**: Displays per-metric judge averages (`1–5`), pass rates (`>= 3` and `>= 4`), and an interactive, sortable CSV table highlighting failing rows.
* **Re-run Workflows**: Re-run a completed pipeline either **from scratch** (full Data Generation + Indexing + Analysis) or **report only** (re-runs Analysis against the existing GraphRAG index).
* **Interactive Code Chat**: Slide-out chat panel (`POST /api/v2/queries`) that queries any single-repo or multi-repo GraphRAG knowledge graph in MLflow and caches downloaded indexes locally per pod for fast follow-up questions.
* **Multi-Namespace Switching & Light/Dark Theme**: Switch between OpenShift namespaces (`KFP_NAMESPACE`) from the top toolbar and toggle light/dark mode.
* **Disconnected / Air-Gapped Ready**: Vendor assets (`mermaid.min.js`, `gridjs`) and the offline `tiktoken` cache (`cl100k_base.tiktoken`) are baked into the container image with `UV_OFFLINE=1` and `PIP_NO_INDEX=1`.

---

## 2. Prerequisites & Configuration

1. **Cluster Prerequisites**:
   * Logged into your OpenShift cluster (`oc login ...`).
   * Base AUGUR resources, secrets (`code-understanding-env`), MLflow assets, and KFP pipelines deployed in `KFP_NAMESPACE` (via `make install`).
2. **Environment Variables (`.env`)**:
   Ensure the following settings are present in `.env` (see [`.env.template`](.env.template)):

   ```dotenv
   KFP_NAMESPACE=<your-openshift-project>
   KFP_IMAGE_REGISTRY=quay.io/<your-org>

   # Optional overrides (defaults shown)
   CONSOLE_APP_IMAGE_NAME=console-app
   CONSOLE_PLUGIN_IMAGE_NAME=console-plugin
   CONSOLE_IMAGE_TAG=latest
   ```

---

## 3. How to Build and Deploy

You can run the console in three ways depending on your workflow:

### Option A: OpenShift Web Console Plugin (Recommended)

Deploys both the Nginx dynamic plugin (`Deployment/code-understanding-console-plugin`) and the HTTPS FastAPI backend (`Deployment/code-understanding-plugin-api`) from [`resources/helm/templates/console-plugin.yaml`](resources/helm/templates/console-plugin.yaml), registers the `ConsolePlugin` resource, and enables it in the OpenShift Console operator.

#### Using an External Container Registry (`podman`)

1. **Build and push the console images**:
   ```bash
   # Build & push the FastAPI console backend + UI image
   make build-console-app-image

   # Build & push the OpenShift dynamic plugin image
   make build-console-plugin-image
   make push-console-plugin-image
   ```
   *(Tip: `make build-push-all` builds and pushes all four KFP pipeline images plus both console images in one command.)*

2. **Deploy and enable the plugin in OpenShift**:
   ```bash
   make deploy-console-plugin
   ```

#### Using OpenShift In-Cluster Binary Builds (No External Registry Needed)

To build both images directly inside OpenShift (`BuildConfig` -> internal OpenShift image registry) and deploy them in one step:

```bash
make oc-deploy-console-plugin
```

#### Accessing the Plugin

Once `make deploy-console-plugin` (or `make oc-deploy-console-plugin`) finishes rolling out:
* Open `https://<openshift-console-host>/code-understanding`, **or**
* Click the OpenShift **Application Launcher** (9-dot grid icon in the top masthead) and select **Code Understanding**.

---

### Option B: Standalone Console Deployment (Port-Forward)

Deploys `Deployment/code-understanding-console` and `Service/code-understanding-console` (`8080`) in `KFP_NAMESPACE` from [`resources/helm/templates/console-app.yaml`](resources/helm/templates/console-app.yaml) without modifying cluster-scoped OpenShift Console settings.

1. **Build and deploy**:
   ```bash
   # Build & push with podman, then deploy:
   make build-console-app-image
   make deploy-console-app

   # OR build in-cluster via OpenShift binary BuildConfig and deploy:
   make oc-deploy-console-app
   ```

2. **Port-forward and open locally**:
   ```bash
   make port-forward-console-app
   ```
   Then open <http://localhost:8080> in your browser.

---

### Option C: Run Locally for Development (`uv` + `uvicorn`)

Runs the FastAPI app directly on your workstation against your OpenShift cluster's `ds-pipeline-dspa` Route and `oc whoami --show-token`:

```bash
make run-console-app
```

Then open <http://127.0.0.1:8080>.

---

## 4. Updating After Code Changes

When you modify files under [`ui/`](ui/), [`api/`](api/), [`console-plugin/`](console-plugin/), or [`workflows/examples/code_understanding/`](workflows/examples/code_understanding/):

1. Rebuild and push the updated image(s) (`make build-console-app-image`, and `make build-console-plugin-image && make push-console-plugin-image` if [`console-plugin/`](console-plugin/) changed).
2. Re-run `make deploy-console-plugin` (or `make deploy-console-app`) so OpenShift restarts the deployment pods and pulls the updated image.
   *(Or run `make oc-deploy-console-plugin` / `make oc-deploy-console-app` to rebuild in-cluster and roll out in one command.)*
