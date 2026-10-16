#!/usr/bin/env bash
# Exits 0 (skip) if pipelines are already uploaded to KFP, 1 (proceed) otherwise.
# When invoked with --with-guidellm-check, --guidellm-only, or GUIDELLM_PRECHECK_ENABLED=true,
# runs a fast GuideLLM concurrent readiness & SLO probe against the configured LLM endpoint.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

clean_val() {
    local v="${1%%#*}"
    v="${v//$'\r'/}"
    v="${v#"${v%%[![:space:]]*}"}"
    echo "${v%"${v##*[![:space:]]}"}"
}

RUN_GUIDELLM="$(clean_val "${GUIDELLM_PRECHECK_ENABLED:-false}")"
GUIDELLM_ONLY="false"

for arg in "$@"; do
    case "$arg" in
        --with-guidellm-check) RUN_GUIDELLM="true" ;;
        --guidellm-only)       RUN_GUIDELLM="true"; GUIDELLM_ONLY="true" ;;
    esac
done

if [[ "${RUN_GUIDELLM,,}" =~ ^(true|1|yes)$ ]]; then
    echo "==> Running Fast GuideLLM Pre-Flight Probe..."
    bash "$SCRIPT_DIR/run_guidellm.sh" \
        --role "$(clean_val "${GUIDELLM_TARGET_ROLE:-graphrag}")" \
        --workload "precheck" \
        --rates "$(clean_val "${GUIDELLM_PRECHECK_CONCURRENCY:-5}")" \
        --max-seconds "$(clean_val "${GUIDELLM_PRECHECK_SECONDS:-15}")" \
        --env-export "/tmp/guidellm_autotune.env" \
        --enforce-slo
    PROBE_RC=$?
    if [[ $PROBE_RC -ne 0 ]]; then
        echo "Error: Fast GuideLLM Pre-Flight Probe failed (exit code $PROBE_RC)." >&2
        exit $PROBE_RC
    fi
    if [[ -f "/tmp/guidellm_autotune.env" ]]; then
        # shellcheck disable=SC1091
        source "/tmp/guidellm_autotune.env"
    fi
    if [[ "$GUIDELLM_ONLY" == "true" ]]; then
        exit 0
    fi
fi

python3 - <<'PYEOF'
import os, sys, json, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import kfp_server_api.configuration as _kfp_conf
_kfp_conf.Configuration.verify_ssl = property(lambda self: False, lambda self, v: None)
from kfp.client import Client

with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as _f:
    _token = _f.read().strip()
client = Client(host=os.environ["KFP_HOST"], existing_token=_token)
result = client.list_pipelines(filter=json.dumps({
    "predicates": [{"key": "display_name", "operation": "EQUALS", "stringValue": "single_repo"}]
}))
if result.pipelines:
    print("Pipelines already uploaded, skipping.")
    sys.exit(0)
sys.exit(1)
PYEOF
