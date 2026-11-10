FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /app

USER 0

COPY resources/images/data-analysis/requirements.txt /tmp/analysis-requirements.txt
RUN pip install --no-cache-dir -r /tmp/analysis-requirements.txt \
    && pip install --no-cache-dir \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "kfp>=2.7,<3" \
        "kfp-kubernetes>=1.2" \
        "kubernetes>=28.1" \
    && rm -f /tmp/analysis-requirements.txt

COPY --chown=1001:0 workflows/examples/code_understanding /app/workflows/examples/code_understanding
COPY --chown=1001:0 api /app/api
COPY --chown=1001:0 ui  /app/ui

ENV PYTHONPATH=/app:/app/workflows/examples/code_understanding \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOME=/tmp

USER 1001

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "ui.main:app", "--host", "0.0.0.0", "--port", "8080"]
