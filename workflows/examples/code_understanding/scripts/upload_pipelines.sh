#!/usr/bin/env bash
# Compiles kubeflow_generation.ipynb and uploads all KFP pipeline templates.
# Intended to run inside the upload-kubeflow-pipelines Kubernetes job where
# the service account token is available for KFP authentication.
#
# Environment variables:
#   KFP_NAMESPACE  Kubernetes namespace, used to derive KFP_HOST (required)
#   KFP_HOST       Override the KFP endpoint (default: ds-pipeline-dspa in-cluster URL)

set -euo pipefail

if [[ -z "${KFP_NAMESPACE:-}" ]]; then
    echo "Error: KFP_NAMESPACE must be set and non-empty." >&2
    exit 1
fi

KFP_HOST="${KFP_HOST:-https://ds-pipeline-dspa.${KFP_NAMESPACE}.svc.cluster.local:8443}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_UNDERSTANDING_DIR="$(dirname "$SCRIPT_DIR")"
COMPILED_DIR="$SCRIPT_DIR/compiled_pipelines"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

TEMP_SCRIPT="$COMPILED_DIR/${TIMESTAMP}_kubeflow_generation.py"
YAML_DIR="$COMPILED_DIR/${TIMESTAMP}_yamls"

mkdir -p "$COMPILED_DIR"

# ---------------------------------------------------------------------------
# Compile notebook to YAML pipeline definitions
# ---------------------------------------------------------------------------
echo "Converting kubeflow_generation.ipynb to script..."
jupyter nbconvert --to python \
    "$CODE_UNDERSTANDING_DIR/utils/notebooks/kubeflow_generation.ipynb" \
    --output "$COMPILED_DIR/${TIMESTAMP}_kubeflow_generation"

echo "Compiling all pipelines..."
PIPELINE_COMPILE_ONLY=1 \
KFP_PIPELINE_OUTPUT_DIR="$YAML_DIR" \
PYTHONPATH="$CODE_UNDERSTANDING_DIR:${PYTHONPATH:-}" \
python3 "$TEMP_SCRIPT"
echo "  Compiled YAMLs -> $YAML_DIR/"

rm -f "$TEMP_SCRIPT"

# ---------------------------------------------------------------------------
# upload_pipeline
#   Uploads a compiled YAML to KFP as a reusable template.
#   Adds a new version if the pipeline already exists.
#
#   $1  yaml           path to the compiled pipeline YAML
#   $2  pipeline_name  name to register the pipeline under in KFP
# ---------------------------------------------------------------------------
upload_pipeline() {
    local yaml="$1"
    local pipeline_name="$2"
    echo "Uploading $pipeline_name to $KFP_HOST..."
    KFP_UPLOAD_YAML="$yaml" \
    KFP_UPLOAD_NAME="$pipeline_name" \
    python3 <<PYEOF
import os, sys, json, urllib3, kfp_server_api.configuration as _kfp_conf, kfp
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as _f:
    token = _f.read().strip()
client = kfp.Client(host="$KFP_HOST", namespace="$KFP_NAMESPACE", existing_token=token)
yaml_path = os.environ["KFP_UPLOAD_YAML"]
pipeline_name = os.environ["KFP_UPLOAD_NAME"]
try:
    pipeline = client.upload_pipeline(
        pipeline_package_path=yaml_path,
        pipeline_name=pipeline_name,
    )
    print(f"  Uploaded pipeline id: {pipeline.pipeline_id}")
except Exception as e:
    if getattr(e, "status", None) == 409:
        from datetime import datetime
        result = client.list_pipelines(filter=json.dumps({
            "predicates": [{"key": "display_name", "operation": "EQUALS", "stringValue": pipeline_name}]
        }))
        pipeline_id = result.pipelines[0].pipeline_id
        version = client.upload_pipeline_version(
            pipeline_package_path=yaml_path,
            pipeline_version_name=datetime.utcnow().strftime("%Y%m%d%H%M%S"),
            pipeline_id=pipeline_id,
        )
        print(f"  Uploaded new version id: {version.pipeline_version_id}")
    else:
        raise
PYEOF
    echo "  OK: $pipeline_name uploaded."
}

upload_pipeline "$YAML_DIR/single_full.yaml" "single_full"
upload_pipeline "$YAML_DIR/aggregated.yaml"  "aggregated"

echo "All pipelines uploaded."
