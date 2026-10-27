# Container Image Build Process

## Pull request builds (GitHub Actions)

The [PR container images workflow](../../.github/workflows/pr-images.yml) runs
when a pull request is opened, reopened, or updated with new commits. It builds
the PR head commit for Linux AMD64, discovering every immediate subdirectory of
`resources/images` that contains a `Containerfile`, including `pipeline-tools`.
Every eligible image is built, regardless of which files changed.

Builds run in parallel without Quay credentials. Each image is saved as a workflow
artifact retained for one day. Only after **all builds succeed** do separate jobs
download those exact images and publish them to Quay. For PR #123, the current
images are published as:

```text
quay.io/ai-shadowman/data-generation:pr123
quay.io/ai-shadowman/data-indexing:pr123
quay.io/ai-shadowman/data-analysis:pr123
quay.io/ai-shadowman/pipeline-tools:pr123
```

Configure these repository secrets under **Settings → Secrets and variables →
Actions** before using automatic publishing:

| Secret | Value |
| --- | --- |
| `QUAY_USERNAME` | Quay username or robot account name, such as `ai-shadowman+github_actions` |
| `QUAY_TOKEN` | Password or robot token with push access to every eligible image repository |

The image repositories must exist under `ai-shadowman`, and the account must have
write access to them. Missing or invalid credentials fail the publishing jobs.
PRs from forks and Dependabot PRs build without publishing because repository
secrets are unavailable to those runs.

New commits cancel superseded runs for that PR. A subsequent successful run
replaces the same `pr<number>` tags; it does not update `latest`. A failed build
prevents all publishing, but a failure during publishing can leave some images
pushed and marks the workflow as failed. Closing a PR does not delete its tags.
Failed jobs can be retried while the image artifacts are retained. After they
expire, re-run all jobs to regenerate them.

New image directories are discovered automatically and use their directory as
the build context. `data-analysis` and `pipeline-tools` use the repository root to
include their workflow package; add a corresponding context override in the
discovery step for any new image that also needs files outside its own directory.

## data-generation

```bash
cd data-generation
podman build -t data-generation:latest .
podman push localhost/data-generation:latest quay.io/ai-shadowman/data-generation:latest 
cd ..
```

## data-indexing

```bash
cd data-indexing
podman build -t data-indexing:latest .
podman push localhost/data-indexing:latest quay.io/ai-shadowman/data-indexing:latest 
cd ..
```

## data-analysis

Build from the repository root so the Containerfile can COPY `workflows/examples/code_understanding`.

```bash
podman build -f resources/images/data-analysis/Containerfile -t data-analysis:latest .
podman push localhost/data-analysis:latest quay.io/ai-shadowman/data-analysis:latest
```

## pipeline-tools

Build from the repository root so the Containerfile can COPY `workflows/examples/code_understanding`.

```bash
podman build -f resources/images/pipeline-tools/Containerfile -t pipeline-tools:latest .
podman push localhost/pipeline-tools:latest quay.io/ai-shadowman/pipeline-tools:latest
```

To use a mirrored UBI base image:

```bash
podman build -f resources/images/pipeline-tools/Containerfile \
  --build-arg BASE_IMAGE=registry.example.com/ubi9/python-311 \
  -t pipeline-tools:latest .
```

## From root all images

```bash
TAG=wip
cd resources/images/data-generation
podman build -t data-generation:$TAG .
podman push localhost/data-generation:$TAG quay.io/ai-shadowman/data-generation:$TAG
cd ../data-indexing
podman build -t data-indexing:$TAG .
podman push localhost/data-indexing:$TAG quay.io/ai-shadowman/data-indexing:$TAG 
cd ../../..
podman build -f resources/images/data-analysis/Containerfile -t data-analysis:$TAG .
podman push localhost/data-analysis:$TAG quay.io/ai-shadowman/data-analysis:$TAG
podman build -f resources/images/pipeline-tools/Containerfile -t pipeline-tools:$TAG .
podman push localhost/pipeline-tools:$TAG quay.io/ai-shadowman/pipeline-tools:$TAG
```

---

## Python Package Build Caching (Layer Optimization)

To prevent redownloading large Python packages across repeated builds:

1. **Native Podman Layer Caching**:
   * Podman caches image layers across builds by default.
   * `requirements.txt` is installed in a dedicated build step:
     ```dockerfile
     ENV PIP_CACHE_DIR=/opt/app-root/src/.cache/pip

     COPY requirements.txt ./requirements.txt

     RUN pip install --upgrade pip \
         && pip install -r requirements.txt
     ```
   * As long as `requirements.txt` does not change, Podman reuses the cached layer (`--> Using cache <layer-id>`), completing in seconds with zero network downloads.
   * Standard Containerfile syntax is preserved (`RUN pip install ...`), ensuring 100% compatibility across all Podman/Buildah versions and OpenShift BuildConfigs.

2. **Optimized Layer Caching in `data-analysis` and `pipeline-tools`**:
   * `requirements.txt` is copied and installed **before** `workflows/examples/code_understanding` is copied.
   * Modifying application code or notebooks only invalidates the final lightweight install step (`pip install ./workflows/examples/code_understanding`), skipping heavy package downloads completely and rebuilding in seconds.
## Building via OpenShift BuildConfig (Cluster-native Builds)

Instead of building locally with Podman, you can offload builds to the OpenShift cluster using `BuildConfig` objects that push directly to `quay.io` using authentication secret `quay-secret`.

### 1. Create the `quay-secret`
```bash
oc create secret docker-registry quay-secret \
  --docker-server=quay.io \
  --docker-username="<QUAY_USERNAME_OR_ROBOT_ACCOUNT>" \
  --docker-password="<QUAY_TOKEN_OR_PASSWORD>" \
  --docker-email="<QUAY_EMAIL>" \
  -n <NAMESPACE>
```

### 2. Deploy BuildConfigs
Deploy using the parameterized template:
```bash
oc process -f resources/openshift/buildconfigs-template.yaml \
  -p AUGUR_GIT_REPO_URL="https://github.com/ai-shadowman/augur.git" \
  -p AUGUR_GIT_REPO_BRANCH="main" \
  -p QUAY_REGISTRY_ORG="ai-shadowman" \
  -p IMAGE_TAG="latest" \
  -p QUAY_SECRET_NAME="quay-secret" \
  -n <NAMESPACE> | oc apply -n <NAMESPACE> -f -
```
Or apply the static manifest:
```bash
oc apply -f resources/openshift/buildconfigs.yaml -n <NAMESPACE>
```

### 3. Start Builds
```bash
oc start-build data-generation -n <NAMESPACE> --follow
oc start-build data-indexing   -n <NAMESPACE> --follow
oc start-build data-analysis   -n <NAMESPACE> --follow
oc start-build pipeline-tools  -n <NAMESPACE> --follow
```


---

## Offline / Disconnected Support: Tiktoken Cache

### Why `cl100k_base.tiktoken` Must Be Baked into Images

GraphRAG and token tracking utilities rely on OpenAI's [`tiktoken`](https://github.com/openai/tiktoken) library for text chunking, token counting, and context window budgeting using the `cl100k_base` BPE (Byte Pair Encoding) tokenizer model.

#### The Problem
* The `tiktoken` Python package **does not bundle tokenizer vocabulary files** in its distribution wheels.
* At runtime, the first time `tiktoken.get_encoding("cl100k_base")` or `tiktoken.encoding_for_model(...)` is invoked, `tiktoken` attempts to download the vocabulary file on the fly over HTTP/HTTPS from:
  ```text
  https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
  ```
* In **disconnected, air-gapped, or firewall-restricted OpenShift/Kubernetes environments**, cluster pods have no outbound route to Microsoft Azure Blob Storage. As a result, the download hangs and fails with `urllib.error.URLError` or connection timeouts, causing GraphRAG text indexing (`create_base_text_units`) and pipeline operations to crash.

#### How Tiktoken Cache Resolution Works
When the `TIKTOKEN_CACHE_DIR` environment variable is defined, `tiktoken` checks that local directory before attempting any network download. It calculates the SHA-1 hash of the remote URL to determine the expected filename:

$$\text{SHA-1}(\text{"https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"}) = \texttt{9b5ad71b2ce5302211f9c61530b329a4922fc6a4}$$

If a file with this exact SHA-1 hash exists inside `TIKTOKEN_CACHE_DIR`, `tiktoken` loads the tokenizer from local disk immediately with **zero external network requests**.

#### Pre-Downloading and Baking into Images
To ensure robust, 100% offline execution across `data-generation`, `data-indexing`, and `data-analysis` container images:

1. **Pre-download the encoding file** to the image directory:
   ```bash
   curl -sSL -o cl100k_base.tiktoken https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken
   ```

2. **Configure the cache in each `Containerfile`**:
   ```dockerfile
   # Tiktoken offline cache setup
   ENV TIKTOKEN_CACHE_DIR=/opt/app-root/src/tiktoken_cache
   RUN mkdir -p /opt/app-root/src/tiktoken_cache
   COPY --chown=1001:0 cl100k_base.tiktoken /opt/app-root/src/tiktoken_cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
   ```

This pre-populates the cache so all subsequent pipeline steps run reliably in fully disconnected and air-gapped clusters.
