#!/usr/bin/env bash
# Runs the GuideLLM concurrency benchmark against the configured LLM endpoint
# and prints the recommended concurrency setting to stdout.
#
# Usage:
#   ./benchmark_llm.sh [--concurrency-levels 1,2,4,8,16,32] [--max-seconds 15] [--max-requests 16]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_UNDERSTANDING_DIR="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(cd "$CODE_UNDERSTANDING_DIR/../../.." && pwd)"

ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

PYTHON_BIN="python3"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

for venv_candidate in "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/venv/bin/python" "/opt/app-root/bin/python3"; do
    if [[ -x "$venv_candidate" ]]; then
        PYTHON_BIN="$venv_candidate"
        break
    fi
done

export PYTHONPATH="${CODE_UNDERSTANDING_DIR}:${PYTHONPATH:-}"

exec "$PYTHON_BIN" -m utils.llm_benchmark "$@"
