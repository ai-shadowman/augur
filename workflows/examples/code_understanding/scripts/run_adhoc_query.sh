#!/usr/bin/env bash
# Query a GraphRAG index with an LLM and save a timestamped result.
#
# Usage:
#   ./scripts/run_adhoc_query.sh "Which modules are riskiest to refactor?"
#   ./scripts/run_adhoc_query.sh "What security vulnerabilities exist?"
#   USE_GLOBAL=0 RETRY_COUNT=5 ./scripts/run_adhoc_query.sh "List dependencies"
#
# Environment variables:
#   USE_GLOBAL   Use global search; set to "0" for local search (default: "1")
#   RETRY_COUNT  Number of retries on failure (default: 3)

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_UNDERSTANDING_DIR="$(dirname "$SCRIPTS_DIR")"

: "${QUESTION:?QUESTION env var is required}"
USE_GLOBAL="${USE_GLOBAL:-1}"
RETRY_COUNT="${RETRY_COUNT:-3}"
GIT_REPO="${GIT_REPO:-}"
GIT_BRANCH="${GIT_BRANCH:-main}"
MULTI_REPO="${MULTI_REPO:-false}"

RESULT=$(CODE_UNDERSTANDING_DIR="$CODE_UNDERSTANDING_DIR" QUESTION="$QUESTION" USE_GLOBAL="$USE_GLOBAL" RETRY_COUNT="$RETRY_COUNT" GIT_REPO="$GIT_REPO" GIT_BRANCH="$GIT_BRANCH" MULTI_REPO="$MULTI_REPO" \
  python3 - << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["CODE_UNDERSTANDING_DIR"])
from pipelines.base.analysis import run_adhoc_query_pipeline
result = run_adhoc_query_pipeline(
    question=os.environ["QUESTION"],
    retry_count=int(os.environ["RETRY_COUNT"]),
    use_global=os.environ["USE_GLOBAL"] == "1",
    git_repo=os.environ.get("GIT_REPO", ""),
    git_branch=os.environ.get("GIT_BRANCH", "main"),
    multi_repo=os.environ.get("MULTI_REPO", "false").lower() == "true",
)
print(result)
PYEOF
)

echo "$RESULT"
