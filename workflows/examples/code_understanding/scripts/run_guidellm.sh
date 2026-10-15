#!/usr/bin/env bash
# Runs GuideLLM benchmarks against AUGUR's configured LLM endpoints (GraphRAG, Ground Truth, Judge),
# logs TTFT/ITL/throughput telemetry to MLflow, and outputs optimal concurrency & rate-limit settings.
#
# Usage:
#   ./run_guidellm.sh [OPTION]...
#
# Options:
#   --role ROLE           Target LLM role: graphrag | ground_truth | judge | all (default: graphrag)
#   --workload WORKLOAD   Workload profile: precheck | sdg | indexing | analysis | all (default: indexing)
#   --profile PROFILE     GuideLLM rate profile: concurrent | sweep | throughput | synchronous
#   --rates RATES         Comma-separated concurrency levels (e.g. "1,5,10,20,25")
#   --max-seconds SEC     Duration per benchmark rate in seconds (default: 30)
#   --output-file FILE    Output JSON path (default: /tmp/augur_guidellm_benchmarks.json)
#   --env-export FILE     Write auto-tuned 'export KEY=VAL' lines to FILE
#   --upload-mlflow       Upload benchmark telemetry and HTML reports to MLflow
#   --enforce-slo         Exit non-zero if throughput < GUIDELLM_MIN_TOK_PER_SEC or error rate == 100%
#   -h, --help            Show this help message

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_UNDERSTANDING_DIR="$(dirname "$SCRIPT_DIR")"

ROLE="${GUIDELLM_TARGET_ROLE:-graphrag}"
WORKLOAD="${GUIDELLM_WORKLOAD:-indexing}"
PROFILE="${GUIDELLM_PROFILE:-}"
RATES="${GUIDELLM_CONCURRENCY_RATES:-}"
MAX_SECONDS="${GUIDELLM_MAX_SECONDS:-0}"
OUTPUT_FILE="${GUIDELLM_OUTPUT_FILE:-/tmp/augur_guidellm_benchmarks.json}"
ENV_EXPORT_FILE="${GUIDELLM_ENV_EXPORT_FILE:-}"
UPLOAD_MLFLOW="${GUIDELLM_UPLOAD_MLFLOW:-true}"
ENFORCE_SLO="${GUIDELLM_ENFORCE_SLO:-false}"

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//' >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role)         ROLE="$2"; shift 2 ;;
        --workload)     WORKLOAD="$2"; shift 2 ;;
        --profile)      PROFILE="$2"; shift 2 ;;
        --rates)        RATES="$2"; shift 2 ;;
        --max-seconds)  MAX_SECONDS="$2"; shift 2 ;;
        --output-file)  OUTPUT_FILE="$2"; shift 2 ;;
        --env-export)   ENV_EXPORT_FILE="$2"; shift 2 ;;
        --upload-mlflow) UPLOAD_MLFLOW="true"; shift ;;
        --no-upload-mlflow) UPLOAD_MLFLOW="false"; shift ;;
        --enforce-slo)  ENFORCE_SLO="true"; shift ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "Error: Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
done

CMD_ARGS=(
    "--role" "$ROLE"
    "--workload" "$WORKLOAD"
    "--output-file" "$OUTPUT_FILE"
)

[[ -n "$PROFILE" ]] && CMD_ARGS+=("--profile" "$PROFILE")
[[ -n "$RATES" ]] && CMD_ARGS+=("--rates" "$RATES")
[[ "$MAX_SECONDS" -gt 0 ]] 2>/dev/null && CMD_ARGS+=("--max-seconds" "$MAX_SECONDS")
[[ -n "$ENV_EXPORT_FILE" ]] && CMD_ARGS+=("--env-export-file" "$ENV_EXPORT_FILE")
[[ "${UPLOAD_MLFLOW,,}" == "true" ]] && CMD_ARGS+=("--upload-mlflow")
[[ "${ENFORCE_SLO,,}" == "true" ]] && CMD_ARGS+=("--enforce-slo")

echo "==> Running AUGUR GuideLLM Benchmark (role=$ROLE, workload=$WORKLOAD)..."
PYTHONPATH="$CODE_UNDERSTANDING_DIR:${PYTHONPATH:-}" \
    python3 -m utils.benchmark_tracker "${CMD_ARGS[@]}"
