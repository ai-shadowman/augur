#!/usr/bin/env bash
# Compiles and uploads all KFP pipeline templates.
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
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
YAML_DIR="$REPO_ROOT/compiled_pipelines/${TIMESTAMP}_yamls"

mkdir -p "$YAML_DIR"

# ---------------------------------------------------------------------------
# Compile all pipelines to YAML
# ---------------------------------------------------------------------------
echo "Compiling all pipelines..."
PIPELINE_COMPILE_ONLY=1 \
KFP_PIPELINE_OUTPUT_DIR="$YAML_DIR" \
PYTHONPATH="$CODE_UNDERSTANDING_DIR:${PYTHONPATH:-}" \
python3 "$CODE_UNDERSTANDING_DIR/pipelines/orchestrator.py"
echo "  Compiled YAMLs -> $YAML_DIR/"

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
    local yaml_size
    yaml_size="$(du -sh "$yaml" | cut -f1)"
    echo "Uploading $pipeline_name ($yaml_size) to $KFP_HOST..."
    KFP_UPLOAD_YAML="$yaml" \
    KFP_UPLOAD_NAME="$pipeline_name" \
    python3 <<PYEOF
import os, sys, json
from datetime import datetime

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import kfp_server_api.configuration as _kfp_conf
_kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
import kfp
import traceback

host          = "$KFP_HOST"
yaml_path     = os.environ["KFP_UPLOAD_YAML"]
pipeline_name = os.environ["KFP_UPLOAD_NAME"]

with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
    token = f.read().strip()

client = kfp.Client(host=host, existing_token=token)

try:
    pipeline = client.upload_pipeline(
        pipeline_package_path=yaml_path,
        pipeline_name=pipeline_name,
    )
    print(f"  Uploaded pipeline id: {pipeline.pipeline_id}")
except Exception as e:
    error_msg = str(e)
    if "already exist" in error_msg.lower() or "409" in error_msg:
        filt = json.dumps({
            "predicates": [{"key": "display_name", "operation": "EQUALS",
                            "string_value": pipeline_name}]
        })
        resp = client.list_pipelines(filter=filt, page_size=1)
        items = resp.pipelines or []
        if not items:
            print(f"  Pipeline '{pipeline_name}' not found after 409", file=sys.stderr)
            sys.exit(1)
        pipeline_id  = items[0].pipeline_id
        version_name = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        version = client.upload_pipeline_version(
            pipeline_package_path=yaml_path,
            pipeline_version_name=version_name,
            pipeline_id=pipeline_id,
        )
        print(f"  Uploaded version id: {version.pipeline_version_id}")
    else:
        print(f"  Upload failed: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
PYEOF
    echo "  OK: $pipeline_name uploaded."
}

# ---------------------------------------------------------------------------
# Auto-discover and upload all compiled YAML files
# ---------------------------------------------------------------------------
for yaml_file in "$YAML_DIR"/*.yaml; do
    [[ -e "$yaml_file" ]] || continue
    pipeline_name="$(basename "$yaml_file" .yaml)"
    upload_pipeline "$yaml_file" "$pipeline_name"
done

echo "All pipelines uploaded."
