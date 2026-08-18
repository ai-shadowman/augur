#!/usr/bin/env bash
# Shortcut for: make run-adhoc-query QUESTION="..."
# Usage: ./wrappers/adhoc.sh <question> [--git-repo URL] [--git-branch BRANCH]

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GIT_REPO=""
GIT_BRANCH=""
QUESTION_PARTS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --git-repo)   GIT_REPO="$2";   shift 2 ;;
        --git-branch) GIT_BRANCH="$2"; shift 2 ;;
        *)            QUESTION_PARTS+=("$1"); shift ;;
    esac
done

[ ${#QUESTION_PARTS[@]} -eq 0 ] && { echo "Usage: $0 <question> [--git-repo URL] [--git-branch BRANCH]" >&2; exit 1; }

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

QUESTION_FILE=$(mktemp)
printf '%s' "${QUESTION_PARTS[*]}" > "$QUESTION_FILE"
TMPFILE=$(mktemp)
trap 'rm -f "$TMPFILE" "$QUESTION_FILE"' EXIT

make -s -C "$REPO_DIR" run-adhoc-query \
    QUESTION_FILE="$QUESTION_FILE" \
    GIT_REPO="$GIT_REPO" \
    GIT_BRANCH="$GIT_BRANCH" \
    >"$TMPFILE" 2>&1 &
MAKE_PID=$!

_spin "$MAKE_PID"
wait "$MAKE_PID"
STATUS=$?

awk '/ADHOC RESULTS/{found=1} found{print}' "$TMPFILE" | while IFS= read -r line; do
    for ((i=0; i<${#line}; i++)); do
        printf '%s' "${line:$i:1}"
        sleep 0.002
    done
    printf '\n'
done
exit $STATUS
