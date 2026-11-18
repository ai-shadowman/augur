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

repo_root() {
    local git_root dir
    git_root="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" || true
    if [[ -n "${git_root:-}" ]]; then
        printf '%s\n' "$git_root"
        return
    fi
    dir="$SCRIPT_DIR"
    while [[ "$dir" != "/" ]]; do
        if [[ -d "$dir/workflows/examples/code_understanding" ]]; then
            printf '%s\n' "$dir"
            return
        fi
        dir="$(dirname "$dir")"
    done
    echo "Error: could not find repository root from $SCRIPT_DIR" >&2
    exit 1
}

REPO_ROOT="$(repo_root)"

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
    KFP_HOST="$KFP_HOST" \
    KFP_UPLOAD_YAML="$yaml" \
    KFP_UPLOAD_NAME="$pipeline_name" \
    PYTHONPATH="$CODE_UNDERSTANDING_DIR:${PYTHONPATH:-}" \
    python3 - <<'PYEOF'
import os
from services.upload_pipeline import upload_pipeline
upload_pipeline(os.environ["KFP_UPLOAD_YAML"], os.environ["KFP_UPLOAD_NAME"])
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
