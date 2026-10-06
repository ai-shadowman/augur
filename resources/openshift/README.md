# OpenShift BuildConfig & Quay.io Build Guide

This directory contains standalone OpenShift build manifests and templates to build all four Augur container images directly on an OpenShift cluster and push them to **Quay.io**.

---

## Architecture Overview

Instead of building images locally using Podman/Docker on a workstation, OpenShift `BuildConfig` objects run build pods directly inside the OpenShift cluster and push images directly to `quay.io`.

The four images built are:
1. **`data-generation`**: Builds from `resources/images/data-generation`.
2. **`data-indexing`**: Builds from `resources/images/data-indexing`.
3. **`data-analysis`**: Builds from repo root `.` (copies `workflows/examples/code_understanding`).
4. **`pipeline-tools`**: Builds from repo root `.` (copies `workflows/examples/code_understanding`).

---

## Step 1: Quay.io Authentication Secret (`quay-secret`)

OpenShift build pods need authentication credentials to push to Quay.io.

### 1. Create the `quay-secret` via the OpenShift CLI:
```bash
oc create secret docker-registry quay-secret \
  --docker-server=quay.io \
  --docker-username="<QUAY_USERNAME_OR_ROBOT>" \
  --docker-password="<QUAY_PASSWORD_OR_TOKEN>" \
  --docker-email="<OPTIONAL_EMAIL>" \
  -n <YOUR_NAMESPACE>
```

> **Note:** If using the Makefile, `<YOUR_NAMESPACE>` should match `KFP_NAMESPACE` in your `.env`.

### 2. (Recommended) Link the secret to the `builder` ServiceAccount:
```bash
oc secrets link builder quay-secret --for=mount -n <YOUR_NAMESPACE>
```

*(Alternatively, you can edit and apply [quay-secret-example.yaml](quay-secret-example.yaml)).*

---

## Step 2: Deploying the BuildConfigs

The BuildConfig definitions are maintained separately from the Makefile and can be deployed using either the parameterized template or the static manifest.

### Option A: Parameterized Template (Recommended)
Allows dynamic parameter configuration for Git repository, branch, Quay organization, and tags:

```bash
oc process -f resources/openshift/buildconfigs-template.yaml \
  -p AUGUR_GIT_REPO_URL="https://github.com/ai-shadowman/augur.git" \
  -p AUGUR_GIT_REPO_BRANCH="main" \
  -p QUAY_REGISTRY_ORG="ai-shadowman" \
  -p IMAGE_TAG="latest" \
  -p QUAY_SECRET_NAME="quay-secret" \
  -n <YOUR_NAMESPACE> | oc apply -n <YOUR_NAMESPACE> -f -
```

### Option B: Static Manifest
Edit the repository and image coordinates in [buildconfigs.yaml](buildconfigs.yaml) and apply directly:
```bash
oc apply -f resources/openshift/buildconfigs.yaml -n <YOUR_NAMESPACE>
```

---

## Step 3: Triggering the Builds

### Option A: Via Makefile (`make oc-build-images`)
A Makefile target is available to kick off all four OpenShift builds concurrently:

```bash
make oc-build-images
```

* **Namespace:** Automatically uses the `KFP_NAMESPACE` defined in your `.env` file.
* **BuildConfig Names:** Automatically aligns with the image names configured in `.env` (`KFP_DATA_GENERATION_BASE_IMAGE_NAME`, `KFP_INDEXING_BASE_IMAGE_NAME`, `KFP_ANALYSIS_BASE_IMAGE_NAME`, `KFP_PIPELINE_TOOLS_IMAGE_NAME`).
* **Streaming Logs:** Pass `BUILD_FLAGS=--follow` to wait and stream build pod output:
  ```bash
  BUILD_FLAGS=--follow make oc-build-images
  ```

### Option B: Via OpenShift CLI Directly
You can start individual builds on the cluster using `oc start-build`:

```bash
oc start-build data-generation -n <YOUR_NAMESPACE> --follow
oc start-build data-indexing   -n <YOUR_NAMESPACE> --follow
oc start-build data-analysis   -n <YOUR_NAMESPACE> --follow
oc start-build pipeline-tools  -n <YOUR_NAMESPACE> --follow
```

### Option C: Binary Builds (Local Workstation Context)
If you have uncommitted or local experimental changes and want OpenShift to build from your local directory without pushing to Git:

```bash
oc start-build data-generation --from-dir=resources/images/data-generation -n <YOUR_NAMESPACE> --follow
oc start-build data-indexing   --from-dir=resources/images/data-indexing -n <YOUR_NAMESPACE> --follow
oc start-build data-analysis   --from-dir=. -n <YOUR_NAMESPACE> --follow
oc start-build pipeline-tools  --from-dir=. -n <YOUR_NAMESPACE> --follow
```

---

## Monitoring Builds

Check build statuses in your namespace:
```bash
oc get builds -n <YOUR_NAMESPACE>
```

Stream logs from an active build:
```bash
oc logs -f build/<BUILD_NAME> -n <YOUR_NAMESPACE>
```
