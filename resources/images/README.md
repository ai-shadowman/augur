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