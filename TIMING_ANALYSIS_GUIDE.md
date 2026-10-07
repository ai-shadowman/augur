# Augur timing collection and offline analysis

This is a local, reviewable engineering example based on Augur commit
`c7c7ce9857b4812e92d668620115d90e364aeee7`. Branch:
`instrumentation/inference-attribution`. The code is undeployed. No live model,
MLflow, storage or cluster measurements were collected for this handoff.

## Purpose and architecture

Answer where an observed run spends time, which requests or uploads are slow,
what overlaps, and which measurements are missing. Begin with a small export
from an existing representative run; use it to decide the next measurement.
The tool does not diagnose a deployment failure or optimize workload settings
automatically. No environment-specific incident history is included.

The implementation has three parts:

1. `utils/request_timing.py` emits opt-in structured events through existing
   Python logging. It adds neither a server nor a telemetry database.
2. `utils/artifact_measurement.py` observes bounded file metadata before an
   attempted MLflow upload. `loaders/mlflow_asset_loader.py` adds operation spans.
3. `scripts/analyze_timing.py` consumes only local exports and writes a static
   browser report, JSON summaries, safe spans CSV and operation CSV. Python's
   standard library is sufficient. No network libraries or remote assets are used.

Keep live infrastructure monitoring in the existing OpenShift
Prometheus/Grafana workflow, and use existing MLflow run metadata/traces when
available. [Upstream MLflow tracing](https://mlflow.org/docs/latest/genai/tracing/)
offers trace views with latency and token usage; actual deployed version,
integration and retained coverage need verification. These JSONL events are
**not automatically MLflow traces**. They do not configure an MLflow trace sink.
An explicit future bridge would require reviewed privacy, sampling, identity,
retention and failure-isolation decisions.

## First local exercise

From `workflows/examples/code_understanding`:

```sh
python3 scripts/analyze_timing.py examples/timing/scenarios.jsonl \
  --metadata examples/timing/controls.json \
  --output-dir /tmp/augur-timing-demo
```

Open `/tmp/augur-timing-demo/report.html` in a browser. It is a standalone file,
requires no server, and contains no JavaScript/CDN assets. The PDF handoff is the
primary reading format; engineers can inspect or print the HTML locally.

If you only have the review ZIP, unzip it and run the bundled standalone CLI
without applying the instrumentation patch. From the unpacked ZIP directory:

```sh
python3 offline-analysis/analyze_timing.py offline-analysis/scenarios.jsonl \
  --metadata offline-analysis/controls.json --output-dir /tmp/augur-zip-demo
```

This exercises synthetic data only. Runtime collection requires the reviewed
instrumentation in the normal application checkout and its existing dependencies.

The fixture has 18 synthetic spans across five scenarios:

| Scenario | What the example demonstrates | What it cannot prove |
| --- | --- | --- |
| `sdk_slow` | A 10-second SDK envelope in a 12-second observed window | Whether queue, network, retries, prefill or decode caused it |
| `io_slow` | A 12-second upload envelope in a 14-second observed window | Actual network throughput or backend storage latency |
| `parallel` | Two overlapping 5-second SDK calls have 6 seconds of interval union | A causal critical path or effective serving replica count |
| `serialized` | Two consecutive 5-second SDK calls have 10 seconds of interval union | Why the caller serialized them or whether parallelism is safe |
| `retry_visible` | A failed SDK envelope, a visible 5-second backoff and another call | Exact internal provider attempts or a network retry rate |

These values test arithmetic and labeling. They are not measurements of Augur,
GPUs, OpenShift or MLflow. Low sample counts intentionally show unknown percentiles.

## Enable, capture and disable for a later authorized run

The patch is off by default. Use INFO logging and one explicit, nonsecret run
identifier across all stages/pods belonging to the same experiment:

```sh
export AUGUR_TIMING_ENABLED=true
export AUGUR_RUN_ID=benchmark_001
export LOGLEVEL=INFO
```

Run the normal, already-approved application entrypoint with stdout/stderr
captured to a local file. For example, replace the entrypoint placeholder before
using this command:

```sh
mkdir -p capture
python3 YOUR_EXISTING_ENTRYPOINT.py > capture/raw.log 2>&1
```

The command above is a collection pattern, not a new application entrypoint.
Container stdout exports can also be analyzed after authorized local export.
This handoff does not execute collection, access a cluster, or change logging
configuration on a running service.

Disable the new events and payload scans with:

```sh
export AUGUR_TIMING_ENABLED=false
unset AUGUR_RUN_ID AUGUR_CLOCK_DOMAIN
```

Only the added timing records are content-free. Augur's preexisting application
logs may include paths, URIs, prompts or error text. Treat raw captures as private;
do not attach or upload them automatically. The analyzer ignores non-event lines
and drops fields outside its allowlist. Inspect approved exports before sharing;
nonsecret identifiers, models and sizes can still reveal operational information.
A syntactic allowlist is not anonymization: even valid identifiers can contain
organization or environment labels. Use approved aliases in shared exports.
Neither collector nor analyzer sends files or installs a metrics backend.

## Toggle matrix and process behavior

| Control | Default / accepted values | Read timing | Effect |
| --- | --- | --- | --- |
| `AUGUR_TIMING_ENABLED` | Off; case-insensitive `true`, `1`, `yes` enable; unset or every other value disables | At each span entry, payload scan gate and LiteLLM timing callback | One global switch for all added timing categories and optional upload metadata scans |
| `LOGLEVEL` | Existing module setup defaults to `INFO` | During module logging setup | Timing emits at INFO; existing logger/filter configuration must allow it. `basicConfig` may do nothing when logging is already configured |
| `AUGUR_RUN_ID` | Optional nonsecret identifier; fallback `PIPELINE_RUN_ID`, then `MLFLOW_RUN_ID` | Per timing event; token tracker captures its own run ID at construction | Correlation only; does not enable collection |
| `AUGUR_CLOCK_DOMAIN` | Unset creates an opaque process-local domain | Per timing event | Clock grouping only; shared value asserts validated synchronization |
| `TOKEN_TRACKER_PRINT_CONSOLE` | On; same `true`/`1`/`yes` parsing; constructor `print_to_console` overrides | At token tracker construction | Existing console summaries only; does not disable usage/cost aggregation or export |
| `CUSTOM_TELEMETRY` / `CUSTOM_EVALUATOR` | Either exact, case-sensitive `mlflow` selects MLflow; otherwise basic no-op provider | Provider construction; MLflow setup initializes once per process | Existing OpenAI autolog and MLflow callback setup; separate from new timing |

There are **no per-category timing environment switches** for SDK, retrieval,
I/O, uploads or callbacks. No general token/cost or DurationTracker disable
environment switch is added. `ASSET_LOADER=mlflow` selects artifact loading,
not metrics; price variables configure cost estimates, not enablement.

Environment variables are per process. Exporting a new value in a shell does
not change an already-running application or pod; set values before launching
the intended process. The timing helper is dynamic within its own process,
but a span already opened while enabled still emits when it finishes. Existing
logger/provider construction settings are not dynamically reconfigured here.

SDK/LiteLLM timing also requires the existing tracker interception to be
registered (`enable_openai_tracking` / `enable_litellm_callbacks`) and supported
by the deployed provider/version. Pipelines currently register those hooks.
Turning timing off preserves those hooks and existing token/cost/duration
behavior. Changing telemetry environment selection does not unregister autolog
hooks already initialized in a running process; this handoff supplies no such
runtime teardown feature. Other pipeline/I/O timing spans use the global flag
directly. Do not confuse hiding console output with disabling collection.

## Run identity, clocks and phase correlation

`run_id` links events across stages. `trace_id`, `span_id` and `parent_span_id`
describe in-process nesting. SDK response `_request_id`, when available, becomes
`provider_request_id`; use it to join approved serving logs. SDK wrapping and
LiteLLM callbacks depend on actual deployed provider/version behavior. Confirm
captured requests against backend logs before claiming complete coverage.

Each process generates an opaque `clock_domain`. The analyzer keeps these axes
separate even when their Unix timestamps appear close. This avoids pretending
that different pods share a clock. Within a domain, bars use seconds relative to
its first valid observed event. The event wall timestamps remain Unix seconds
in CSV; their timezone is UTC by convention, not the operator's local timezone.
Monotonic elapsed is process-local and cannot timestamp events across processes.

Leave `AUGUR_CLOCK_DOMAIN` unset initially. Set a shared nonsecret identifier only
after validating synchronization, offset/drift and acceptable uncertainty across
the participating hosts/processes. This override asserts a common clock domain;
the tool does not verify it. Metadata cannot resynchronize timestamps. A wall/
monotonic difference above max(0.1 seconds, 1% of elapsed) excludes that interval
from timeline union and records a clock issue, while valid monotonic latency may
still be summarized. The threshold is a detection heuristic, not clock accuracy.

Legacy events without an accepted domain are grouped by trace with a warning;
cross-trace/process order remains unknown. Missing parents can result from log
loss, partial export, thread context loss or unsupported boundaries. Parent cycles
and children outside parents are visible diagnostics, not automatically repaired.

Use operation names as current phase labels. No arbitrary phase/tag text is
logged. Backend `mlflow_run_id` identifies the destination run created for an
upload; it is different from the explicit pipeline `run_id`. `experiment_id` and
`prior_mlflow_run_id` are backend identifiers. Capture a separate approved
stage/model-to-pod/GPU role map for multi-process correlation.

Model stages may use different endpoints and resources; verify actual
stage/endpoint/pod placement and routing rather than assuming identical replicas.
Do not put endpoint URLs, credentials or source paths in run IDs or metadata.
All bundled roles, model names and control values are explicitly synthetic.

## Analyze one run or make an explicit comparison

```sh
python3 scripts/analyze_timing.py capture/raw.log \
  --run-id benchmark_001 --output-dir report-one

python3 scripts/analyze_timing.py capture/baseline.log capture/candidate.log \
  --run-id baseline_001 --run-id candidate_001 \
  --metadata capture/effective-controls.json --output-dir report-compare
```

`--run-id` may repeat. Filtering occurs before the retained-event limit, so
unselected runs do not exhaust that budget. Unknown/missing run identity is
bucketed as `unassigned`; use it only for inspecting coverage, not controlled
comparisons. Inputs are local paths; filenames are not copied into outputs.
Outputs may overwrite the four files in the chosen directory; use a fresh
directory when retaining comparisons. The tool performs no automatic uploads.

Provide a local nonsecret manifest; unknown fields and unsafe values are dropped:

```json
{
  "runs": {
    "baseline_001": {
      "source_ref": "approved-corpus-ref",
      "config_id": "effective-config-v1",
      "asset_version": "assets-v1",
      "ignore_rules_id": "ignore-v1",
      "cache_state": "warm",
      "concurrency": 10,
      "accepted_bytes": 4096,
      "accepted_tokens": 1000,
      "success_criteria_id": "metadata-quality-v1",
      "model_role": "example-model-stage",
      "environment_id": "example-environment",
      "serving_topology_id": "approved-role-map-v1",
      "python_version": "3.12",
      "mlflow_version": "deployed-version-id",
      "openai_version": "deployed-version-id",
      "litellm_version": "deployed-version-id",
      "graphrag_version": "deployed-version-id",
      "sdghub_version": "deployed-version-id",
      "image_id": "approved-image-id"
    }
  }
}
```

Replace illustrative values with observed values; never infer them from this
baseline checkout. `benchmark_controls_complete` means ten required keys are
present, not that values are equal, authentic or comparable. Humans must compare
effective controls and document the deliberate variable changed. A separately
approved reference can contain the detailed role map/config; only its safe ID
belongs here. The CLI does not fetch deployed settings or validate a benchmark.

## Outputs and correct interpretation

| Output | Use |
| --- | --- |
| `report.html` | Open locally: expandable run panels, relative timelines, operation/model latency, upload attempts and explicit caveats |
| `summary.json` | Versioned structured summaries, per-domain unions, parser/coverage issues, token distributions and approved controls |
| `spans.csv` | All selected safe retained events for inspection or plotting; retains parent/trace/clock/run identity |
| `operations.csv` | Per-run operation/layer/model/stream latency statistics; empty percentile cells mean unknown |

An observed window is the first-to-last valid observed interval in one clock
domain. It is not automatically a full run duration. The interval union counts
time covered by at least one retained span. Parent spans often cover the entire
window; that says nothing about the cause of each part. Leaf union and window
without leaf spans expose instrumentation coverage gaps. A gap is not proven
idle time, queue wait or model work. SDK and I/O unions can overlap each other;
do not add cards or layer totals into walltime.

No causal critical path is computed. The current schema lacks dependency,
scheduling and completion edges necessary to prove it. Parentage alone is
insufficient, particularly for concurrent branches and asynchronous jobs.
Failed parent/child spans may repeat the same error; failed-span counts are not
unique failed user requests. Visible backoffs and SDK envelopes do not expose
every hidden retry. SDK/LiteLLM rows remain separate, rather than double-counting
their tokens or requests into a combined total. Streaming SDK elapsed stops at
stream creation; it excludes stream consumption. Missing usage is unknown.

Statistics are descriptive, nearest-rank distributions grouped by operation,
layer, model and stream mode. Unknown stream mode is a separate group. Mean,
minimum and maximum appear with any valid sample; p50 requires at least 5,
p95 at least 20, and p99 at least 100. These cutoffs avoid displaying tiny-sample
tail percentiles; they do not provide confidence intervals or establish stable
production tails. Report sample counts, failed calls, warmness and controls.
Do not compare only successful calls while ignoring timeout/backoff periods.

## Upload measurement details

`mlflow.log_results` envelopes the original operation. Child spans separate
experiment lookup, payload measurement, prior-run end, run-create factory,
run tags, upload and run lifecycle. Lifecycle includes context entry/exit;
`mlflow.run_create` times `start_run`, not a separate server create request.
Artifact URI lookup is independent of download. These envelopes include library
work/retries and cannot split MLflow API, network and storage time internally.

Payload fields are `payload_regular_files`, `payload_logical_bytes`,
`payload_scan_entries`, `payload_scan_errors`, `payload_symlinks`,
`payload_scan_complete` and `payload_scan_truncated`. The scanner reads metadata,
not file contents. It follows neither file nor directory symlinks and marks
coverage partial when encountered. Hard-linked paths count separately; logical
file size differs from allocated/compressed/storage bytes. Files can change
between measurement and upload. Concurrent filesystem races remain possible.

The limits are 10,000 visited entries, 250ms cooperative scan budget and 64 open
directory levels. A blocking stat/scandir call can exceed the budget. All open
iterators are closed. Error/limit scans return counts plus partial flags rather
than preventing the original upload. Upload arguments and MLflow context manager
behavior are preserved; exceptions and cancellation propagate unchanged.

`payload_key` hashes the source/destination-path identity with a per-process
random salt. Raw paths never enter timing events. `upload_ordinal` tracks
attempts for that key while its entry remains in a 256-key cache; eviction or
process restart resets correlation. Different processes cannot match keys.
Repeated passes can show an increasing cumulative logical payload sent to new
backend run IDs. That establishes attempted payload shape, not physical
duplication, replication, persisted growth, deduplication or successful partial
transfer. Obtain those from storage/backend measurements.

## Benchmark controls and overhead

First use one small representative approved run. Record raw-log volume, capture
coverage and resource overhead before attempting long runs. New logging is
synchronous and scan work happens before uploads; both can change walltime.
The `mlflow.payload_measurement` span makes scan elapsed visible but does not
account for every logging/formatting cost. The analyzer is offline and contributes
no runtime latency to the captured application.

Compare timing off/on under the same approved corpus, effective model/asset/config,
ignore rules, accepted bytes/tokens, concurrency and quality/completion criteria.
Use repeated runs and preserve variation; do not subtract a single baseline or
declare overhead negligible without evidence. Match cold-versus-warm index/model/
cache state explicitly. Include failures and tail behavior. Use existing log
rotation/retention and a collection budget; the parser's event limit is not a
runtime log-volume limit. Do not add unlimited traces to constrained MLflow storage.

The analyzer accepts at most 1MiB per line and 100,000 selected events (lowerable
with `--max-events`). It counts invalid/extreme numerics, malformed/oversize lines,
duplicates/conflicts and missing timestamps. HTML bounds are 100 run panels,
100 clock panels overall, 300 timeline rows per domain, 200 operation rows and
300 upload rows per run. CSV/JSON retain all safe selected events within the
parser limit. Limiting capture/window size is preferable to enormous reports.

## External evidence and useful next measurements

1. Identify the failing MLflow-related store: exact pod/PVC/mount/error, capacity,
   free bytes/inodes, growth by run, DB tables, artifact inventory and traces.
   Backend metadata and artifact storage are different. Retention/deletion does
   not automatically prove physical reclamation.
2. Align safe run/phase/request IDs with serving queue/prefill/decode, actual
   tokens, replica routing, GPU/KV-cache state and cold-loading. Queue/TTFT/prefill/
   decode are not captured by these SDK wrappers. Do not infer GPU time from SDK
   elapsed or an idle GPU from a leaf coverage gap.
3. In existing OpenShift metrics, align pod startup/scheduling, CPU throttling,
   memory pressure, disk latency/utilization, network throughput/errors, API
   latency and etcd WAL fsync/backend commit/peer RTT/leader changes with I/O bursts.
   Cross-system timeline attribution requires validated clock offsets and IDs.

For model-service-heavy observations, separate queue from computation before
tuning admission/concurrency or output length. For upload-heavy observations,
compare measured payload shape with storage/API/network evidence. For setup gaps,
check downloads, Parquet loading, cache state and orchestration. Do not resize
resources or change retry policies from these examples alone.

Future extensions should remain small and reviewed: explicit provider/local
semaphore wait hooks, backend request/replica IDs, per-attempt retry hooks,
cache-hit indicators, resource-series join adapters, or a sampled MLflow/OTel
bridge. A causal critical-path view needs real dependency/completion edges and
clock uncertainty, not renamed interval union. Put new fields in collector and
parser allowlists, add partial-coverage tests, and preserve failure isolation.

## Review, validation and transfer

From `workflows/examples/code_understanding`:

```sh
python3 -m unittest tests.test_request_timing tests.test_mlflow_timing \
  tests.test_timing_analysis tests.test_token_tracker tests.test_duration_tracker \
  tests.test_kubeflow_utils tests.test_data_generation_telemetry
```

The 156-test suite passed with timing off and on. Tests use local fixtures and
existing optional-dependency stubs; no external calls. Covered cases include SDK
preservation/cancellation, callback lifecycle, bounded/disabled scans, upload/run
failures, cumulative passes, privacy, malformed/extreme inputs, clocks, overlapping
intervals, missing/cyclic parents, percentiles, output limits and explicit filtering.
Live provider/MLflow/container integration and platform measurements remain untested.

The review ZIP contains a binary Git patch plus guides, sample timing, test
evidence, CLI/fixtures and generated synthetic report outputs. Review against the
pinned baseline in a suitable checkout, apply through the team's normal local
workflow, and confirm dependency/provider versions before any authorized release.
Publishing the patch does not deploy it. Live provider/platform validation remains required.
