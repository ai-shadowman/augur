#!/usr/bin/env bash
# Query a GraphRAG index with an LLM and save a timestamped result.
#
# Usage:
#   ./scripts/run_adhoc_query.sh "Which modules are riskiest to refactor?"
#   GRAPHRAG_DIR=graph_rag_app/source ./scripts/run_adhoc_query.sh "What security vulnerabilities exist?"
#   USE_GLOBAL=0 RETRY_COUNT=5 ./scripts/run_adhoc_query.sh "List dependencies"
#
# Environment variables:
#   GRAPHRAG_DIR     GraphRAG root directory containing output/ parquet files (default: ".")
#   USE_GLOBAL   Use global search; set to "0" for local search (default: "1")
#   RETRY_COUNT  Number of retries on failure (default: 3)

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_UNDERSTANDING_DIR="$(dirname "$SCRIPTS_DIR")"

[ $# -lt 1 ] && { echo "Usage: $0 <question>"; exit 1; }

QUESTION="$1"
GRAPHRAG_DIR="${GRAPHRAG_DIR:-graph_rag_app/source}"
[[ "$GRAPHRAG_DIR" != /* ]] && GRAPHRAG_DIR="$CODE_UNDERSTANDING_DIR/$GRAPHRAG_DIR"
USE_GLOBAL="${USE_GLOBAL:-1}"
RETRY_COUNT="${RETRY_COUNT:-3}"

if [ ! -d "$GRAPHRAG_DIR" ]; then
  echo "Error: GRAPHRAG_DIR '$GRAPHRAG_DIR' does not exist. Run the indexing pipeline first." >&2
  exit 1
fi

if [ ! -d "$GRAPHRAG_DIR/output" ] || [ -z "$(ls "$GRAPHRAG_DIR/output"/*.parquet 2>/dev/null)" ]; then
  echo "Error: No GraphRAG output found in '$GRAPHRAG_DIR/output'. Run the indexing pipeline first." >&2
  exit 1
fi

RESULT=$(CODE_UNDERSTANDING_DIR="$CODE_UNDERSTANDING_DIR" QUESTION="$QUESTION" GRAPHRAG_DIR="$GRAPHRAG_DIR" USE_GLOBAL="$USE_GLOBAL" RETRY_COUNT="$RETRY_COUNT" \
  python3 - << 'PYEOF'
import os, sys
sys.path.insert(0, os.environ["CODE_UNDERSTANDING_DIR"])
from pipelines.base.analysis import run_adhoc_query_pipeline
result = run_adhoc_query_pipeline(
    graphrag_source_path=os.environ["GRAPHRAG_DIR"],
    question=os.environ["QUESTION"],
    retry_count=int(os.environ["RETRY_COUNT"]),
    use_global=os.environ["USE_GLOBAL"] == "1",
)
print(result)
PYEOF
)

echo "$RESULT"
