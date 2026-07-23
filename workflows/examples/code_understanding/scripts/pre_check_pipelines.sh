#!/usr/bin/env bash
# Exits 0 (skip) if pipelines are already uploaded to KFP, 1 (proceed) otherwise.

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
    "predicates": [{"key": "display_name", "operation": "EQUALS", "stringValue": "single_full"}]
}))
if result.pipelines:
    print("Pipelines already uploaded, skipping.")
    sys.exit(0)
sys.exit(1)
PYEOF
