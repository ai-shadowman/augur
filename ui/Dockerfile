FROM registry.access.redhat.com/ubi9/python-311:latest

USER root
ARG UV_VERSION=0.12.11
RUN python3 -m pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /opt/app-root/src

COPY ui/pyproject.toml .
COPY ui/uv.lock .
RUN uv sync --frozen --no-install-project --no-dev

COPY ui/ .
COPY ui/ /opt/app-root/ui/
COPY api/ /opt/app-root/api/
COPY workflows/examples/code_understanding/ /opt/app-root/workflows/examples/code_understanding/

ENV PYTHONPATH=/opt/app-root:/opt/app-root/src:/opt/app-root/workflows/examples/code_understanding \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

USER 1001
EXPOSE 8080
CMD ["/opt/app-root/src/.venv/bin/python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
