# Container Image Build Process

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