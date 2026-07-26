#!/usr/bin/env bash
# Compiles all KFP pipelines and submits or uploads the requested ones.
#
# Usage:
#   ./run_pipelines.sh --single-repo      # run single-repo pipeline (data generation → indexing → analysis)
#   ./run_pipelines.sh --multi-repo       # run multi-repo data generation pipeline
#   ./run_pipelines.sh --upload-single-repo     # upload single-repo pipeline template to KFP (no run)
#   ./run_pipelines.sh --upload-multi-repo      # upload multi-repo pipeline template to KFP (no run)
#
# Environment variables:
#   KFP_NAMESPACE         Kubernetes namespace (KFP_HOST is derived from this)
#   KFP_IMAGE_REGISTRY    Image registry that hosts pipeline images (required)
#   KFP_DATA_GENERATION_OUTPUT_PATH  Output path from data generation (default: target)
#   KFP_DATA_INDEXING_OUTPUT_PATH    GraphRAG source path for indexing and analysis (default: graph_rag_app/source)

set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [OPTION]...

Options:
  --single-repo         Run single-repo pipeline (data generation -> indexing -> analysis)
  --multi-repo          Run multi-repo data generation pipeline
  --upload-single-repo  Upload single-repo pipeline template to KFP (no run)
  --upload-multi-repo   Upload multi-repo pipeline template to KFP (no run)
  -h, --help            Show this help message

Environment variables:
  KFP_NAMESPACE         Kubernetes namespace, used to derive KFP_HOST (required)
  KFP_IMAGE_REGISTRY    Image registry that hosts pipeline images (required)
  KFP_DATA_GENERATION_OUTPUT_PATH  Output path from data generation (default: target)
  KFP_DATA_INDEXING_OUTPUT_PATH    GraphRAG source path for indexing and analysis (default: graph_rag_app/source)
EOF
}

if [[ -z "${KFP_NAMESPACE:-}" ]]; then
    echo "Error: KFP_NAMESPACE must be set and non-empty." >&2
    exit 1
fi

if [[ -z "${KFP_IMAGE_REGISTRY:-}" ]]; then
    echo "Error: KFP_IMAGE_REGISTRY must be set and non-empty." >&2
    exit 1
fi

KFP_HOST="${KFP_HOST:-https://ds-pipeline-dspa.${KFP_NAMESPACE}.svc.cluster.local:8443}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPILED_DIR="$SCRIPT_DIR/compiled_pipelines"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
YAML_DIR="$COMPILED_DIR/${TIMESTAMP}_yamls"
TARGET_PATH="${KFP_DATA_GENERATION_OUTPUT_PATH:-target}"
GRAPHRAG_SOURCE_PATH="${KFP_DATA_INDEXING_OUTPUT_PATH:-graph_rag_app/source}"

mkdir -p "$YAML_DIR"

# ---------------------------------------------------------------------------
# Compile all pipelines to YAML
# ---------------------------------------------------------------------------
echo "Compiling all pipelines..."
PIPELINE_COMPILE_ONLY=1 \
KFP_PIPELINE_OUTPUT_DIR="$YAML_DIR" \
PYTHONPATH="$SCRIPT_DIR:${PYTHONPATH:-}" \
python3 "$SCRIPT_DIR/pipelines/full_pipelines.py"
echo "  Compiled YAMLs -> $YAML_DIR/"

# ---------------------------------------------------------------------------
# submit_pipeline
#   Submits a compiled YAML directly to Kubeflow Pipelines.
#
#   $1  yaml      path to the compiled pipeline YAML
#   $2  run_name  name for the KFP run
#   $3  params    optional JSON string of run parameters (default: {})
# ---------------------------------------------------------------------------
submit_pipeline() {
    local yaml="$1"
    local run_name="$2"
    local params="${3:-{}}"
    echo "Submitting $run_name to $KFP_HOST..."
    python3 - "$yaml" "$run_name" "$params" <<PYEOF
import sys, json, urllib3, kfp_server_api.configuration as _kfp_conf, kfp
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as _f:
    _token = _f.read().strip()
client = kfp.Client(host="$KFP_HOST", namespace="$KFP_NAMESPACE", existing_token=_token)
run = client.create_run_from_pipeline_package(
    pipeline_file=sys.argv[1],
    run_name=sys.argv[2],
    params=json.loads(sys.argv[3]),
    enable_caching=False,
)
print(f"  Submitted run id: {run.run_id}")
PYEOF
    echo "  OK: $run_name submitted."
}

# ---------------------------------------------------------------------------
# upload_pipeline
#   Uploads a compiled YAML to Kubeflow Pipelines as a reusable template.
#
#   $1  yaml           path to the compiled pipeline YAML
#   $2  pipeline_name  name to register the pipeline under in KFP
# ---------------------------------------------------------------------------
upload_pipeline() {
    local yaml="$1"
    local pipeline_name="$2"
    echo "Uploading $pipeline_name to $KFP_HOST..."
    python3 - "$yaml" "$pipeline_name" <<PYEOF
import sys, json, urllib3, kfp_server_api.configuration as _kfp_conf, kfp
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as _f:
    _token = _f.read().strip()
client = kfp.Client(host="$KFP_HOST", namespace="$KFP_NAMESPACE", existing_token=_token)
try:
    pipeline = client.upload_pipeline(
        pipeline_package_path=sys.argv[1],
        pipeline_name=sys.argv[2],
    )
    print(f"  Uploaded pipeline id: {pipeline.pipeline_id}")
except Exception as e:
    if getattr(e, "status", None) == 409:
        from datetime import datetime
        result = client.list_pipelines(filter=json.dumps({
            "predicates": [{"key": "display_name", "operation": "EQUALS", "stringValue": sys.argv[2]}]
        }))
        pipeline_id = result.pipelines[0].pipeline_id
        version = client.upload_pipeline_version(
            pipeline_package_path=sys.argv[1],
            pipeline_version_name=datetime.utcnow().strftime("%Y%m%d%H%M%S"),
            pipeline_id=pipeline_id,
        )
        print(f"  Uploaded new version id: {version.pipeline_version_id}")
    else:
        raise
PYEOF
    echo "  OK: $pipeline_name uploaded."
}

RUN_SINGLE_REPO=false
RUN_MULTI_REPO=false
UPLOAD_SINGLE_REPO=false
UPLOAD_MULTI_REPO=false

if [[ $# -eq 0 ]]; then
    RUN_SINGLE_REPO=true
    RUN_MULTI_REPO=true
fi

for arg in "$@"; do
    case "$arg" in
        --single-repo)        RUN_SINGLE_REPO=true ;;
        --multi-repo)         RUN_MULTI_REPO=true ;;
        --upload-single-repo) UPLOAD_SINGLE_REPO=true ;;
        --upload-multi-repo)  UPLOAD_MULTI_REPO=true ;;
        -h|--help)            usage; exit 0 ;;
        *) echo "Error: Unknown argument: $arg" >&2; usage; exit 1 ;;
    esac
done

$RUN_SINGLE_REPO   && submit_pipeline "$YAML_DIR/single_repo.yaml" "single_repo_${TIMESTAMP}" \
    "{\"target_path\": \"$TARGET_PATH\", \"graphrag_source_path\": \"$GRAPHRAG_SOURCE_PATH\"}"

$RUN_MULTI_REPO    && submit_pipeline "$YAML_DIR/multi_repo.yaml" "multi_repo_${TIMESTAMP}"

$UPLOAD_SINGLE_REPO && upload_pipeline "$YAML_DIR/single_repo.yaml" "single_repo"
$UPLOAD_MULTI_REPO  && upload_pipeline "$YAML_DIR/multi_repo.yaml"  "multi_repo"

echo "All done."
