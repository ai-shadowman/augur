FROM registry.access.redhat.com/ubi9/python-311:latest

USER root
WORKDIR /opt/app-root/src

ENV PYTHONPATH=/opt/app-root:/opt/app-root/src:/opt/app-root/workflows/examples/code_understanding \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    TIKTOKEN_CACHE_DIR=/opt/app-root/src/tiktoken_cache \
    UV_NO_CACHE=1 \
    PIP_NO_CACHE_DIR=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True \
    LITELLM_TELEMETRY=False \
    DISABLE_LITELLM_VERSION_CHECK=True \
    GRAPHRAG_LOCAL_QUERY_SKIP_TLS_VERIFY=true \
    MLFLOW_TRACKING_INSECURE_TLS=true

# 1. Static offline tiktoken cache & /opt/app-root/ui symlink (avoids duplicating ui/ files)
COPY --chown=1001:0 resources/images/data-analysis/cl100k_base.tiktoken /opt/app-root/src/tiktoken_cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
RUN ln -sfn /opt/app-root/src /opt/app-root/ui

# 2. Isolated dependency layer: only rebuilds when ui/pyproject.toml or ui/uv.lock changes.
#    Permissions on .venv are finalised here so code-only changes never touch the ~1.5 GB .venv layer.
ARG UV_VERSION=0.12.11
COPY --chown=1001:0 ui/pyproject.toml ui/uv.lock ./
RUN python3 -m pip install --no-cache-dir "uv==${UV_VERSION}" \
    && uv sync --frozen --no-install-project --no-dev --no-cache \
    && uv pip install --python /opt/app-root/src/.venv/bin/python --no-cache \
        "pathspec>=1.1.1,<2" \
        "pip-requirements-parser>=32,<33" \
        "opentelemetry-exporter-otlp-proto-http>=1.40.0" \
    && python3 -m pip uninstall -y uv \
    && rm -rf /root/.cache /tmp/* \
    && chgrp -R 0 /opt/app-root && chmod -R g=u /opt/app-root

# 3. Enforce offline package managers at container runtime
ENV UV_OFFLINE=1 \
    PIP_NO_INDEX=1

# 4. Application source layers ordered from least- to most-frequently changed.
#    Only app code permissions are updated so the cached .venv layer is reused on push.
COPY --chown=1001:0 workflows/examples/code_understanding/ /opt/app-root/workflows/examples/code_understanding/
COPY --chown=1001:0 api/ /opt/app-root/api/
COPY --chown=1001:0 ui/ /opt/app-root/src/
RUN find /opt/app-root/src -mindepth 1 -maxdepth 1 ! -name .venv ! -name tiktoken_cache -exec chgrp -R 0 {} + \
    && find /opt/app-root/src -mindepth 1 -maxdepth 1 ! -name .venv ! -name tiktoken_cache -exec chmod -R g=u {} + \
    && chgrp -R 0 /opt/app-root/api /opt/app-root/workflows \
    && chmod -R g=u /opt/app-root/api /opt/app-root/workflows

USER 1001
EXPOSE 8080
CMD ["/opt/app-root/src/.venv/bin/python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
