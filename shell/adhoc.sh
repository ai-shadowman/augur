#!/usr/bin/env bash
# Shortcut for: make run-adhoc-query QUESTION="..."
# Usage: ./shell/adhoc.sh Which modules are riskiest to refactor?

[ $# -lt 1 ] && { echo "Usage: $0 <question>" >&2; exit 1; }

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

_spin() {
    local pid=$1
    local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0
    local n=${#frames}
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r%s Querying..." "${frames:$((i % n)):1}"
        i=$((i + 1))
        sleep 0.1
    done
    printf "\r\033[K"
}

TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE"' EXIT

make -s -C "$REPO_DIR" run-adhoc-query QUESTION="$*" >"$TMPFILE" 2>&1 &
MAKE_PID=$!

_spin "$MAKE_PID"
wait "$MAKE_PID"
STATUS=$?

cat "$TMPFILE"
exit $STATUS
