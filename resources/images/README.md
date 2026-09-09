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