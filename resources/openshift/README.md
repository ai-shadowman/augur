# OpenShift BuildConfig & Quay.io Build Guide

This directory contains standalone OpenShift build manifests and templates to build all four Augur container images directly on an OpenShift cluster and push them to **Quay.io**.

This process is completely decoupled from the local `make build-images` workflow.

---

## Prerequisites: Quay Secret (`quay-secret`)

OpenShift build pods need authentication credentials to push to Quay.io.

### 1. Create `quay-secret` via OpenShift CLI:
```bash
oc create secret docker-registry quay-secret \
  --docker-server=quay.io \
  --docker-username="<QUAY_USERNAME_OR_ROBOT>" \
  --docker-password="<QUAY_PASSWORD_OR_TOKEN>" \
  --docker-email="<OPTIONAL_EMAIL>" \
  -n <YOUR_NAMESPACE>
```

### 2. (Recommended) Link the secret to the `builder` ServiceAccount:
```bash
oc secrets link builder quay-secret --for=mount -n <YOUR_NAMESPACE>
```

Alternatively, you can edit and apply [quay-secret-example.yaml](quay-secret-example.yaml).

---

## Deploying the BuildConfigs

You can deploy the BuildConfigs either using the **parameterized Template** or the **static manifest**.

### Option A: Parameterized Template (Recommended)
Customize Git repository, branch, Quay organization, and tags:

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
Edit target image repositories in [buildconfigs.yaml](buildconfigs.yaml) and apply:
```bash
oc apply -f resources/openshift/buildconfigs.yaml -n <YOUR_NAMESPACE>
```

---

## Triggering the Builds

### Trigger cluster Git builds:
```bash
oc start-build data-generation -n <YOUR_NAMESPACE> --follow
oc start-build data-indexing   -n <YOUR_NAMESPACE> --follow
oc start-build data-analysis   -n <YOUR_NAMESPACE> --follow
oc start-build pipeline-tools  -n <YOUR_NAMESPACE> --follow
```

### Or trigger local binary builds (without committing/pushing to Git):
```bash
oc start-build data-generation --from-dir=resources/images/data-generation -n <YOUR_NAMESPACE> --follow
oc start-build data-indexing   --from-dir=resources/images/data-indexing -n <YOUR_NAMESPACE> --follow
oc start-build data-analysis   --from-dir=. -n <YOUR_NAMESPACE> --follow
oc start-build pipeline-tools  --from-dir=. -n <YOUR_NAMESPACE> --follow
```
