# TLS Certificate Injection for Pipeline Jobs

Pipeline and asset jobs talk to in-cluster HTTPS endpoints (MLflow, Kubeflow Pipelines, S3-compatible storage) that present OpenShift-issued certificates. This branch adds a single switch so those jobs either **skip TLS verification** or **trust the cluster CA bundle**.

**Prerequisite:** Namespace is created with the Open Data Hub dashboard label so OpenShift AI injects `odh-trusted-ca-bundle`. See [Tola's Harness Installation](tolas-harness-installation.md).

## Contents

1. [How TLS is enabled](#1-how-tls-is-enabled)
2. [What the cluster must provide](#2-what-the-cluster-must-provide)
3. [What gets injected](#3-what-gets-injected)
4. [Jobs that honor the switch](#4-jobs-that-honor-the-switch)
5. [Default (verification off)](#5-default-verification-off)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. How TLS is enabled

The operator-facing flag is `ENABLE_TLS_VERIFICATION` in `.env`. Helm receives it as `tls.enableVerification`.

Default is **off** (skip verification):

```bash
# .env (from .env.template)
ENABLE_TLS_VERIFICATION=false
```

To **enable** verification and inject the cluster CA:

```bash
sed -i "s/ENABLE_TLS_VERIFICATION=.*/ENABLE_TLS_VERIFICATION=true/" .env
```

`make install`, `make upload-pipelines`, `make upload-mlflow-assets`, `make run-pipelines`, and `make run-adhoc-query` all pass the value through:

```bash
--set tls.enableVerification="${ENABLE_TLS_VERIFICATION:-false}"
```

`resources/helm/values.yaml` defaults the same way:

```yaml
tls:
  enableVerification: false
```

Set the env var **before** `make install` (and again before any later job target). Helm templates the Job specs at apply time; changing `.env` does not retroactively patch running or already-created Jobs.

---

## 2. What the cluster must provide

When the Data Science project namespace is created, it is labeled `opendatahub.io/dashboard: "true"`. OpenShift AI then injects ConfigMap `odh-trusted-ca-bundle` with key `ca-bundle.crt` (cluster trusted CAs, including the service CA).

`make install` waits until that bundle is present before Helm upgrade:

```bash
until oc get configmap odh-trusted-ca-bundle -n $KFP_NAMESPACE \
  -o jsonpath='{.data.ca-bundle\.crt}' 2>/dev/null | grep -q CERTIFICATE; do sleep 5; done
```

`make run-adhoc-query` refuses to start if verification is on and the ConfigMap is missing:

```text
Error: TLS settings issue. Contact your system administrator.
```

Confirm the bundle exists:

```bash
oc get configmap odh-trusted-ca-bundle -n $KFP_NAMESPACE
oc get configmap odh-trusted-ca-bundle -n $KFP_NAMESPACE -o jsonpath='{.data.ca-bundle\.crt}' | head
```

---

## 3. What gets injected

When `tls.enableVerification` is **true**, each affected Job:

1. Mounts `odh-trusted-ca-bundle` key `ca-bundle.crt` as file `tls-ca-bundle.pem`.
2. Mounts that volume at `/etc/pki/ca-trust/extracted/pem` (read-only).
3. Points TLS-aware clients at that file:

| Environment variable | Used by |
| --- | --- |
| `REQUESTS_CA_BUNDLE` | Python `requests` / urllib |
| `SSL_CERT_FILE` | OpenSSL-based clients |
| `AWS_CA_BUNDLE` | boto3 / MinIO S3 |
| `MLFLOW_TRACKING_SERVER_CERT_PATH` | MLflow tracking client |

Workbench notebooks already mount `workbench-trusted-ca-bundle` independently and are **not** gated by `ENABLE_TLS_VERIFICATION`.

---

## 4. Jobs that honor the switch

| Helm template | Make target |
| --- | --- |
| `resources/helm/templates/upload-pipelines-job.yaml` | `make upload-pipelines` / `make install` |
| `resources/helm/templates/upload-assets-job.yaml` | `make upload-mlflow-assets` |
| `resources/helm/templates/run-pipelines-job.yaml` | `make run-pipelines` |
| `resources/helm/templates/run-adhoc-query-job.yaml` | `make run-adhoc-query` |

---

## 5. Default (verification off)

When `ENABLE_TLS_VERIFICATION=false` (or unset):

- Jobs do **not** mount `odh-trusted-ca-bundle`.
- `MLFLOW_TRACKING_INSECURE_TLS=true` is set so the MLflow client skips certificate checks.
- Adhoc query jobs also set `GRAPHRAG_LOCAL_QUERY_SKIP_TLS_VERIFY=true`.

Use this for demo clusters or when the ODH CA bundle is not available. Prefer `ENABLE_TLS_VERIFICATION=true` on clusters that issue private or service-CA certificates and have injected `odh-trusted-ca-bundle`.

---

## 6. Troubleshooting

**Jobs fail with certificate verify errors after enabling TLS**

- Confirm `ENABLE_TLS_VERIFICATION=true` was set when the Job was applied (`oc get job <name> -o yaml` and inspect env / volumeMounts).
- Confirm `odh-trusted-ca-bundle` contains a PEM block (`-----BEGIN CERTIFICATE-----`).
- Re-run the Make target so Helm re-templates the Job.

**Adhoc query exits immediately with a TLS settings error**

- The ConfigMap is missing in `$KFP_NAMESPACE`. Re-run `make install` and wait for ODH injection, or set `ENABLE_TLS_VERIFICATION=false`.

**Verification looks enabled but env still has `MLFLOW_TRACKING_INSECURE_TLS`**

- Helm treats only YAML/ `--set` booleans `true`/`false`. Keep the `.env` value exactly `true` or `false` (no quotes inside the file).
