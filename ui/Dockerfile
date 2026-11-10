FROM registry.access.redhat.com/ubi9/python-311:latest

USER root
ARG UV_VERSION=0.12.11
RUN python3 -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /opt/app-root/src

# Tiktoken offline cache setup (prevents runtime download from openaipublic.blob.core.windows.net)
ENV TIKTOKEN_CACHE_DIR=/opt/app-root/src/tiktoken_cache
RUN mkdir -p /opt/app-root/src/tiktoken_cache
COPY --chown=1001:0 resources/images/data-analysis/cl100k_base.tiktoken /opt/app-root/src/tiktoken_cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4

COPY ui/pyproject.toml .
COPY ui/uv.lock .
RUN uv sync --frozen --no-install-project --no-dev \
    && uv pip install --python /opt/app-root/src/.venv/bin/python --no-cache \
        "pathspec>=1.1.1,<2" \
        "pip-requirements-parser>=32,<33" \
        "opentelemetry-exporter-otlp-proto-http>=1.40.0"

COPY ui/ .
COPY ui/ /opt/app-root/ui/
COPY api/ /opt/app-root/api/
COPY workflows/examples/code_understanding/ /opt/app-root/workflows/examples/code_understanding/
RUN uv pip install --python /opt/app-root/src/.venv/bin/python --no-cache --no-deps /opt/app-root/workflows/examples/code_understanding \
    && chgrp -R 0 /opt/app-root && chmod -R g=u /opt/app-root

ENV PYTHONPATH=/opt/app-root:/opt/app-root/src:/opt/app-root/workflows/examples/code_understanding \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    UV_OFFLINE=1 \
    PIP_NO_INDEX=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    LITELLM_TELEMETRY=False \
    DISABLE_LITELLM_VERSION_CHECK=True \
    GRAPHRAG_LOCAL_QUERY_SKIP_TLS_VERIFY=true \
    MLFLOW_TRACKING_INSECURE_TLS=true

USER 1001
EXPOSE 8080
CMD ["/opt/app-root/src/.venv/bin/python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
