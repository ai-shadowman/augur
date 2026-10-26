# Local Augur timing example

## Overview

Use this patch to see where a run spends time. It adds timing events for model
calls, retrieval, setup and artifact operations. A local tool turns exported
events into an HTML report and CSV/JSON files. The patch does not fix performance
problems automatically.

Timing is off by default. Before you start the application, set
`AUGUR_TIMING_ENABLED=true` and allow INFO logs. To disable the added events and
payload scans, set `AUGUR_TIMING_ENABLED=false` before the next run. These shell
changes do not update a running application. Existing token/cost tracking and
MLflow autolog have separate controls.

SDK elapsed time is not GPU compute time. Upload counts describe logical file
bytes, not bytes sent over the network or retained in storage. Spans can overlap;
do not add their durations. Run IDs and model names can reveal operational
information. The field allowlist does not make exports anonymous.

Try the tool with synthetic data from `workflows/examples/code_understanding`:

```sh
python3 scripts/analyze_timing.py examples/timing/scenarios.jsonl \
  --metadata examples/timing/controls.json --output-dir /tmp/augur-timing-demo
```

Open `/tmp/augur-timing-demo/report.html` in a browser. This example makes no
model, MLflow or cluster calls. See the [quickstart](TIMING_ANALYSIS_GUIDE.md#first-local-exercise)
and [toggle matrix](TIMING_ANALYSIS_GUIDE.md#toggle-matrix-and-process-behavior)
for the detailed steps and controls.

## Technical reference

Base: `c7c7ce9857b4812e92d668620115d90e364aeee7` from `ai-shadowman/augur` main.
Branch: `instrumentation/inference-attribution`. Local validation used no deployment,
cluster access or live model call. The patch and offline analyzer need no
new Python packages; PDF production dependencies live outside the repository.

This patch adds opt-in, content-free JSON timing events to existing call sites.
It leaves concurrency, prompts, retries, model configuration and existing
DurationTracker/token/cost schemas unchanged. It uses only Python's standard
library. It does not fix the hypothesized bottlenecks.

## What the diff demonstrates

Before, direct SDK calls recorded successful token usage but no timing or
failure event. The illustrative change is:

```python
# before
resp = await tracker_self._orig_async_chat(*args, **kwargs)

# after (sync chat and sync/async embeddings follow the same pattern)
with timing_span("openai.chat", "sdk", model=kwargs.get("model"),
                 stream_requested=bool(kwargs.get("stream", False))) as event:
    resp = await tracker_self._orig_async_chat(*args, **kwargs)
    event.update(response_usage(resp))
```

The original response/exception flows back to its caller. Each invocation gets
a unique span ID, parent/trace IDs, start/end wall timestamps, monotonic elapsed
seconds and status. A failure logs its exception class, never its message.
Actual response usage and optional SDK `_request_id` are included when available;
unknown token values are omitted rather than estimated. All events are marked
`additive: false`; they are not inserted into aggregate duration or cost records.

One small callback lifecycle correction is necessary: repeated enable calls now
remove this tracker's old handlers before registering their replacements. This
prevents duplicate timing/usage callbacks and ensures disable removes them;
other integrations such as MLflow remain registered. It can correct preexisting
duplicate token counts when the same tracker was enabled repeatedly.

The other new locations are:

| File | Added coverage |
| --- | --- |
| `utils/token_tracker.py` | Existing LiteLLM success callback timestamps plus failure callbacks; sync/async SDK chat and embeddings |
| `utils/graphrag_utils.py` | Query invocation, direct model/global/local search envelopes, outer retry backoff, full report, five Parquet reads |
| `pipelines/base/analysis.py` | Full ad hoc invocation, index download and analyzer setup before the old query timer |
| `pipelines/base/data_generation.py` | SDG batch envelope |
| `pipelines/graphrag.py` | GraphRAG index-build envelope |
| `utils/kubeflow_utils.py` | Gzip extraction and archive creation, compressed archive bytes |
| `loaders/mlflow_asset_loader.py` | Artifact URI lookup, download/copy, full log_results, experiment lookup, prior-run end, run-create factory/lifecycle, tags and upload |
| `utils/artifact_measurement.py` | Bounded metadata-only payload counts and process-local opaque upload correlation |
| `scripts/analyze_timing.py` | Local JSONL analysis to standalone HTML, safe event/operation CSV and JSON summaries |
| `utils/request_timing.py` | Small shared helper, safe field allowlist, context-local nesting |

## Reviewing and enabling later

The patch is off by default. For an authorized local/container run, set
`AUGUR_TIMING_ENABLED=true` and use INFO logging. Set the same nonsecret
`AUGUR_RUN_ID` across stages when available to correlate separate pods. These
instructions were not applied to a running environment. No new telemetry
backend or credentials are required: events go to `augur.timing` in existing
Python logs with the prefix `AUGUR_TIMING`.

One global switch controls the added categories; per-category switches do not
exist. Accepted on values are case-insensitive `true`, `1`, `yes`; the default is
off. Export values before launching the intended process: shell changes do not
update a running pod/application. See the toggle matrix in
[TIMING_ANALYSIS_GUIDE.md](TIMING_ANALYSIS_GUIDE.md) for dynamic timing gates,
startup-only logging/provider settings, existing console controls and hook
registration dependencies. Turning timing off does not disable existing
token/cost aggregation, DurationTracker or MLflow autolog.

The added events do not log prompts, answers, source contents, URLs, paths,
headers, credentials or exception messages. Existing Augur logging is unchanged;
this patch does not make the application's preexisting logs content-free.

Do not add existing cost/autolog records to the new events as extra model work.
LiteLLM and direct SDK layers may report the same underlying call, and parent
and child spans overlap. Use operation/layer, parent/trace IDs, timestamps and
optional provider request IDs to inspect a timeline, not a sum. Threaded provider
callbacks may not inherit contextvars; the explicit run ID still identifies the
run, while their exact parent may be absent. This is an initial logging patch,
not a complete distributed tracing integration or a guaranteed one-event-per-wire-
attempt counter.

## Inference versus other time

The SDK span is **client SDK elapsed**, including transport and opaque SDK
retries; it is not GPU inference time. SDG's local semaphore/queue, internal
retry attempts, GraphRAG map/reduce fan-out, and actual cache hits need the
deployed library/provider hooks. Successful and failed LiteLLM callbacks are
observable only when the installed LiteLLM version/provider invokes them.
Nested OpenAI wrapping is also provider-dependent; validate captured calls
against backend logs. MLflow callbacks/autolog remain registered.

Normal nonstreaming calls cannot report TTFT/prefill/decode separately here.
For `stream=True`, the SDK event ends when the stream object is created and is
explicitly flagged; it does not time stream consumption. Serving-side request
queue/prefill/decode metrics (or a separately reviewed streaming integration)
are needed. Missing server timing fields must not be presented as zero.
The optional provider request ID can be joined to serving logs where supported;
the patch does not invent replica identity or change request routing.

For any target environment, record stage/model/endpoint/pod/node placement,
actual replica counts, routing, queue depth and in-flight requests. Different
model stages are not interchangeable replicas, and intended capacity does not
prove useful request distribution. This handoff contains no site configuration
or deployed hardware assumptions. Storage media alone does not demonstrate
isolation between workload I/O and control-plane services.

The archive events report bytes/time so transfer scale can be compared with
storage/network measurements. `mlflow.artifact_download` times the download
library call; the preceding experiment/run/artifact lookup has its own
`mlflow.artifact_uri_lookup` span.
It cannot separate network time from storage wait. Obtain object
sizes, network bytes/errors, storage latency and node placement externally.
Model cold-loading also needs serving startup metrics. This code alone cannot
attribute a delay to network, accelerator or storage hardware.

## Assessment priorities and proposed instrumentation gaps

Begin with the observed failure or slow stage in the target environment.
For storage investigations, distinguish backend metadata, artifacts and traces;
collect the failing service/volume/error, free bytes/inodes, table sizes and
retained growth by run. No deployed capacity or failure history is included.
[MLflow distinguishes backend metadata from artifacts](https://mlflow.org/docs/latest/self-hosting/architecture/backend-store/).
Public Helm defaults are source configuration, not observations of a deployment.

Code evidence at the pinned baseline: `pipelines/base/data_generation.py:648`
loops languages/config passes. Each nonempty pass saves into the same target
directory, then `:549` logs the entire cumulative directory. With the MLflow
loader, `loaders/mlflow_asset_loader.py:201` starts a new run and `:234` calls
log_artifacts. Earlier files can consequently be uploaded again in later passes.
Indexing logs its output and KFP archives it separately; telemetry JSON has direct
and catalog upload paths. Measure actual payloads and physical storage growth;
the repeated-write hypothesis does not establish a runtime failure mechanism.

The local extension now instruments MLflow run creation/lifecycle and uploads.
Each upload carries safe backend run/experiment IDs, elapsed/status, observed
regular-file counts/logical bytes, and an opaque process-local payload key/ordinal.
Measurement is opt-in, metadata-only, skips symlinks, and is bounded to 10,000
entries, 250ms cooperative budget and 64 directory levels. Incomplete/error scans
are flagged as lower bounds; a blocking filesystem call can exceed the time budget.
These are attempted logical payloads, not actual network bytes or retained storage.
Repeated totals cannot establish replication, deduplication or a storage failure.

Persistent volume/DB high-water growth, backend API waits, autolog trace volume,
retention/reclamation, pod scheduling, etcd and serving queue/prefill/decode remain
external measurements. The new spans do not diagnose their causes. Coordinate
collection budgets with existing MLflow and OpenShift observability.

The etcd/shared-resource hypothesis is plausible but requires correlated
node/disk/API/pod metrics. Inspect etcd WAL fsync, backend commit, peer RTT and
leader changes around I/O bursts; do not diagnose quorum from GPU idleness alone.
[Red Hat's etcd guidance](https://docs.redhat.com/en/documentation/openshift_container_platform/4.18/html/scalability_and_performance/recommended-performance-and-scalability-practices-2)
describes these shared-I/O and latency risks. No cluster commands or load tests
were run. The illustrative logging patch is insufficient to establish them. The inspected
KFP wrappers do not explicitly set per-task CPU/memory scheduling constraints;
namespace admission defaults and deployed pod specs are unknown.

For comparisons, use identical approved source/config/model, ignore rules,
accepted bytes/tokens, cache/warmness/concurrency and success criteria. Planning
estimates are not measured throughput and do not establish linear scaling.
Changing host topology, resource sharing and network paths together confounds
causal attribution. Availability and storage redundancy must be assessed
separately. No infrastructure migration or production experiment is proposed here.
The patch remains undeployed; provider/platform extensions remain proposed.

## Validation

From `workflows/examples/code_understanding`:

```sh
python3 -m unittest tests.test_request_timing tests.test_token_tracker \
  tests.test_mlflow_timing tests.test_timing_analysis \
  tests.test_duration_tracker tests.test_kubeflow_utils \
  tests.test_data_generation_telemetry
```

The 156-test suite passed with timing off and with timing enabled. It uses the existing dependency
stubs and fake SDK modules: no external network or paid model calls. New tests
cover all four SDK paths, failures/cancellation, response/exception preservation,
concurrent parents, unique repeated IDs, opt-in/privacy, callback/autolog
preservation, unchanged retry delay, ad hoc setup nesting and archive contents.
Existing cost/duration tests continue to pass. Syntax compilation and
`git diff --check` are also required. Container/library integration and live
serving validation remain unperformed.

The exported `sample-timing.jsonl` comes from deterministic mock calls and is
illustrative evidence of event shape, not a performance measurement.

## Offline analysis handoff

See [TIMING_ANALYSIS_GUIDE.md](TIMING_ANALYSIS_GUIDE.md) for complete collection,
comparison, interpretation, clock, overhead and extension guidance. From
`workflows/examples/code_understanding`, generate a synthetic browser-open report:

```sh
python3 scripts/analyze_timing.py examples/timing/scenarios.jsonl \
  --metadata examples/timing/controls.json --output-dir /tmp/augur-timing-demo
```

Open `/tmp/augur-timing-demo/report.html` in a browser. It requires no server,
JavaScript, CDN or connection. Use `summary.json`, `spans.csv` and `operations.csv`
for downstream work. The PDF handoff is the readable primary document. Scenario
times are synthetic correctness fixtures, not measured deployment performance.
The analyzer does not calculate a causal critical path or bridge logs into MLflow.
