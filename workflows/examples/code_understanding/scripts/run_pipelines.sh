#!/usr/bin/env bash
# Triggers existing KFP pipeline runs by name.
# Pipelines must be uploaded first via: make upload-pipelines
#
# Usage:
#   ./run_pipelines.sh --single-repo   # trigger single-repo pipeline run
#   ./run_pipelines.sh                 # trigger multi-repo pipeline run (default)
#   ./run_pipelines.sh --multi-repo    # trigger multi-repo pipeline run
#
# Environment variables:
#   KFP_NAMESPACE         Kubernetes namespace (KFP_HOST is derived from this)
#   GIT_REPO              Git repository URL to analyse (required for --single-repo)
#   GIT_BRANCH            Git branch to clone (default: main)
#   PARENT_SOURCE_PATH                 Parent source path (default: source)
#   PARENT_TARGET_PATH                 Parent target path (default: target)
#   KFP_DATA_INDEXING_OUTPUT_PATH      GraphRAG base path (default: graph_rag_app/source)

set -euo pipefail

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [OPTION]...

Options:
  --single-repo   Trigger single-repo pipeline run (GIT_REPO required)
  --multi-repo    Trigger multi-repo pipeline run (default)
  -h, --help      Show this help message

Environment variables:
  KFP_NAMESPACE  Kubernetes namespace, used to derive KFP_HOST (required)
  GIT_REPO       Git repository URL to analyse (required for --single-repo)
  GIT_BRANCH     Git branch to clone (default: main)
  PARENT_SOURCE_PATH                Parent source path (default: source)
  PARENT_TARGET_PATH                Parent target path (default: target)
  KFP_DATA_INDEXING_OUTPUT_PATH     GraphRAG base path (default: graph_rag_app/source)
EOF
}

if [[ -z "${KFP_NAMESPACE:-}" ]]; then
    echo "Error: KFP_NAMESPACE must be set and non-empty." >&2
    exit 1
fi

KFP_HOST="${KFP_HOST:-https://ds-pipeline-dspa.${KFP_NAMESPACE}.svc.cluster.local:8443}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
GIT_REPO="${GIT_REPO:-}"
GIT_BRANCH="${GIT_BRANCH:-main}"
SOURCE_PATH="${PARENT_SOURCE_PATH:-source}"
TARGET_PATH="${PARENT_TARGET_PATH:-target}"
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

total = client.list_pipeline_versions(pipeline_id=pipeline_id, page_size=1).total_size or 1
versions = client.list_pipeline_versions(pipeline_id=pipeline_id, page_size=total)
if not versions.pipeline_versions:
    print(f"Error: pipeline '{pipeline_name}' has no versions in KFP.", file=sys.stderr)
    sys.exit(1)
latest_version = sorted(versions.pipeline_versions, key=lambda v: v.created_at, reverse=True)[0]
version_id = latest_version.pipeline_version_id

experiment = client.create_experiment(name="Default")
run = client.run_pipeline(
    experiment_id=experiment.experiment_id,
    job_name=run_name,
    pipeline_id=pipeline_id,
    version_id=version_id,
    params=params,
    enable_caching=False,
)
print(f"  Submitted run id: {run.run_id}")
PYEOF
    echo "  OK: $run_name submitted."
}

MODE="multi"

for arg in "$@"; do
    case "$arg" in
        --single-repo) MODE="single" ;;
        --multi-repo)  MODE="multi" ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "Error: Unknown argument: $arg" >&2; usage; exit 1 ;;
    esac
done

if [[ "$MODE" == "single" ]]; then
    if [[ -z "$GIT_REPO" ]]; then
        echo "Error: GIT_REPO must be set and non-empty for --single-repo." >&2
        exit 1
    fi
    trigger_pipeline "single_repo" "single_repo_${TIMESTAMP}" \
        "{\"git_repo\": \"$GIT_REPO\", \"git_branch\": \"$GIT_BRANCH\", \"parent_source_path\": \"$SOURCE_PATH\", \"parent_target_path\": \"$TARGET_PATH\"}"
else
    trigger_pipeline "multi_repo" "multi_repo_${TIMESTAMP}" \
        "{\"parent_source_path\": \"$SOURCE_PATH\", \"parent_target_path\": \"$TARGET_PATH\"}"
fi

echo "All done."
