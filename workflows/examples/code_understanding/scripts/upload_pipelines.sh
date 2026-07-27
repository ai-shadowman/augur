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
python3 "$CODE_UNDERSTANDING_DIR/pipelines/full_pipelines.py"
echo "  Compiled YAMLs -> $YAML_DIR/"

# ---------------------------------------------------------------------------
# preflight_check
#   Verifies the KFP API server is reachable and accepting requests before
#   any upload is attempted.  Prints the HTTP status and body so failures
#   can be diagnosed without needing to check server logs separately.
# ---------------------------------------------------------------------------
preflight_check() {
    echo "Running preflight check against $KFP_HOST..."
    python3 <<PYEOF
import json, os, ssl, sys, urllib.request

host = "$KFP_HOST"
token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"

with open(token_path) as f:
    token = f.read().strip()

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

url = f"{host}/apis/v2beta1/pipelines?page_size=1"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

try:
    with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
        status = resp.status
        body = resp.read().decode("utf-8", errors="replace")[:200]
        print(f"  Preflight GET {url} -> HTTP {status}")
        print(f"  Response snippet: {body}")
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")[:500]
    print(f"  Preflight GET {url} -> HTTP {e.code} ({e.reason})", file=sys.stderr)
    print(f"  Response body: {body}", file=sys.stderr)
    print(f"  Hint: check 'oc logs -l app=ds-pipeline-dspa -n $KFP_NAMESPACE --all-containers'", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"  Preflight failed: {e}", file=sys.stderr)
    print(f"  Hint: check 'oc logs -l app=ds-pipeline-dspa -n $KFP_NAMESPACE --all-containers'", file=sys.stderr)
    sys.exit(1)
PYEOF
}

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
    status = getattr(e, "status", None)
    if status == 409:
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
        body = getattr(e, "body", None)
        print(f"  Upload failed: HTTP {status} - {getattr(e, 'reason', e)}", file=sys.stderr)
        if body:
            print(f"  Response body: {body[:500]}", file=sys.stderr)
        print(f"  Hint: check 'oc logs -l app=ds-pipeline-dspa -n $KFP_NAMESPACE --all-containers'", file=sys.stderr)
        raise
PYEOF
    echo "  OK: $pipeline_name uploaded."
}

# ---------------------------------------------------------------------------
# Auto-discover and upload all compiled YAML files
# ---------------------------------------------------------------------------
preflight_check

for yaml_file in "$YAML_DIR"/*.yaml; do
    [[ -e "$yaml_file" ]] || continue
    pipeline_name="$(basename "$yaml_file" .yaml)"
    upload_pipeline "$yaml_file" "$pipeline_name"
done

echo "All pipelines uploaded."
