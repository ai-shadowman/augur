#!/usr/bin/env bash
# Triggers existing KFP pipeline runs by name.
# Pipelines must be uploaded first via: make upload-pipelines
#
# Usage:
#   ./run_pipelines.sh --single      # trigger existing single-repo pipeline run
#   ./run_pipelines.sh --aggregated  # trigger existing aggregated pipeline run
#
# Environment variables:
#   KFP_NAMESPACE         Kubernetes namespace (KFP_HOST is derived from this)
#   KFP_DATA_GENERATION_OUTPUT_PATH  Output path from data generation (default: target)
#   KFP_DATA_INDEXING_OUTPUT_PATH    GraphRAG source path for indexing (default: graph_rag_app/source)

set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [OPTION]...

Options:
  --single      Trigger existing single-repo pipeline run
  --aggregated  Trigger existing aggregated pipeline run
  -h, --help    Show this help message

Environment variables:
  KFP_NAMESPACE  Kubernetes namespace, used to derive KFP_HOST (required)
  KFP_DATA_GENERATION_OUTPUT_PATH  Output path from data generation (default: target)
  KFP_DATA_INDEXING_OUTPUT_PATH    GraphRAG source path for indexing (default: graph_rag_app/source)
EOF
}

if [[ -z "${KFP_NAMESPACE:-}" ]]; then
    echo "Error: KFP_NAMESPACE must be set and non-empty." >&2
    exit 1
fi

KFP_HOST="${KFP_HOST:-https://ds-pipeline-dspa.${KFP_NAMESPACE}.svc.cluster.local:8443}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
TARGET_PATH="${KFP_DATA_GENERATION_OUTPUT_PATH:-target}"
GRAPHRAG_SOURCE_PATH="${KFP_DATA_INDEXING_OUTPUT_PATH:-graph_rag_app/source}"

# ---------------------------------------------------------------------------
# trigger_pipeline
#   Looks up an existing pipeline by name in KFP and creates a new run.
#   Errors clearly if the pipeline has not been uploaded yet.
#
#   $1  pipeline_name  name of the registered pipeline in KFP
#   $2  run_name       name for the new run
#   $3  params         optional JSON string of run parameters (default: {})
# ---------------------------------------------------------------------------
trigger_pipeline() {
    local pipeline_name="$1"
    local run_name="$2"
    local params="$3"
    [ -z "$params" ] && params='{}'
    echo "Triggering $pipeline_name as $run_name on $KFP_HOST..."
    # Pass pipeline_name, run_name, and params via env vars to avoid shell
    # quoting issues when the JSON params string is passed as a CLI argument.
    KFP_TRIGGER_PIPELINE="$pipeline_name" \
    KFP_TRIGGER_RUN="$run_name" \
    KFP_TRIGGER_PARAMS="$params" \
    python3 <<PYEOF
import os, sys, json, subprocess, urllib3, kfp_server_api.configuration as _kfp_conf, kfp
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
_SA = "/var/run/secrets/kubernetes.io/serviceaccount/token"
if os.path.exists(_SA):
    with open(_SA) as _f:
        token = _f.read().strip()
else:
    token = subprocess.check_output(["oc", "whoami", "--show-token"], text=True).strip()
client = kfp.Client(host="$KFP_HOST", namespace="$KFP_NAMESPACE", existing_token=token)

pipeline_name = os.environ["KFP_TRIGGER_PIPELINE"]
run_name = os.environ["KFP_TRIGGER_RUN"]
params = json.loads(os.environ["KFP_TRIGGER_PARAMS"])

result = client.list_pipelines(filter=json.dumps({
    "predicates": [{"key": "display_name", "operation": "EQUALS", "stringValue": pipeline_name}]
}))
if not result.pipelines:
    print(f"Error: pipeline '{pipeline_name}' not found in KFP.", file=sys.stderr)
    print("Upload it first with: make upload-pipelines", file=sys.stderr)
    sys.exit(1)

pipeline_id = result.pipelines[0].pipeline_id
experiment = client.create_experiment(name="Default")
run = client.run_pipeline(
    experiment_id=experiment.experiment_id,
    job_name=run_name,
    pipeline_id=pipeline_id,
    params=params,
    enable_caching=False,
)
print(f"  Submitted run id: {run.run_id}")
PYEOF
    echo "  OK: $run_name submitted."
}

RUN_SINGLE=false
RUN_AGGREGATED=false

if [[ $# -eq 0 ]]; then
    RUN_SINGLE=true
    RUN_AGGREGATED=true
fi

for arg in "$@"; do
    case "$arg" in
        --single)     RUN_SINGLE=true ;;
        --aggregated) RUN_AGGREGATED=true ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Error: Unknown argument: $arg" >&2; usage; exit 1 ;;
    esac
done

if $RUN_SINGLE; then
    trigger_pipeline "single_full" "single_full_${TIMESTAMP}" \
        "{\"target_path\": \"$TARGET_PATH\", \"graphrag_source_path\": \"$GRAPHRAG_SOURCE_PATH\"}"
fi

$RUN_AGGREGATED && trigger_pipeline "aggregated" "aggregated_${TIMESTAMP}"

echo "All done."
