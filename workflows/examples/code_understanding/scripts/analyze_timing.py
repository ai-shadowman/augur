#!/usr/bin/env python3
"""Offline, standard-library analysis of Augur timing exports. No network I/O.

Only a fixed event/metadata allowlist is retained. Intervals describe observed
occupancy, never causal critical paths. SDK time is not GPU computation time.
"""
import argparse
import csv
import html
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

OPERATIONS = set("""pipeline.run openai.chat openai.embeddings litellm.request
graphrag.query graphrag.direct_model graphrag.global_search graphrag.local_search
graphrag.retry_backoff graphrag.build_index analysis.report analysis.adhoc_invocation
analysis.index_download analysis.analyzer_setup sdg.generate_batch artifact.archive
artifact.extract index.read.entities index.read.relationships index.read.text_units
index.read.communities index.read.community_reports mlflow.artifact_uri_lookup
mlflow.artifact_download mlflow.artifact_copy mlflow.log_results mlflow.experiment_lookup
mlflow.payload_measurement mlflow.prior_run_end mlflow.run_lifecycle mlflow.run_create
mlflow.run_tags mlflow.artifact_upload""".split())
LAYERS = {"application", "sdk", "litellm", "provider", "retrieval", "retry", "batch", "io", "mlflow"}
IDS = {"span_id", "parent_span_id", "trace_id", "run_id", "clock_domain", "provider_request_id",
       "payload_key", "mlflow_run_id", "prior_mlflow_run_id", "experiment_id"}
NUMBERS = {"start_time", "end_time", "elapsed_seconds", "prompt_tokens", "output_tokens",
           "attempts_remaining", "bytes", "payload_regular_files", "payload_logical_bytes",
           "payload_scan_entries", "payload_scan_errors", "payload_symlinks", "upload_ordinal"}
BOOLS = {"stream_requested", "payload_scan_complete", "payload_scan_truncated"}
CONTROLS = {"source_ref", "config_id", "asset_version", "ignore_rules_id", "cache_state",
            "concurrency", "accepted_bytes", "accepted_tokens", "success_criteria_id", "model_role"}
METADATA = CONTROLS | {"environment_id", "clock_domain", "python_version", "mlflow_version",
                      "openai_version", "litellm_version", "graphrag_version", "sdghub_version",
                      "image_id", "serving_topology_id"}
MAX_LINE_BYTES = 1024 * 1024
MAX_EVENTS = 100000
MAX_HTML_RUNS = 100
MAX_HTML_CLOCKS = 100
LIMITATIONS = [
    "No causal critical path is calculated: dependency/completion edges are absent.",
    "Nested spans and SDK/LiteLLM layers overlap; never sum them as elapsed run time or model work.",
    "Each clock domain has its own relative axis. Cross-domain ordering and union are unknown.",
    "Unknown legacy clocks are grouped by trace only; this does not establish synchronized processes.",
    "SDK elapsed includes queue/transport/provider retries; GPU time, queue/prefill/decode and TTFT are unknown.",
    "Failures and backoffs are observed envelopes, not exact network attempts or a retry rate.",
    "Payload counts are regular-file logical bytes observed before attempted upload, not network or persisted bytes.",
    "Partial/symlink scans are lower bounds. Repeated counts do not prove duplication, replication or storage growth.",
    "Missing events, clock adjustments, dropped logs and warmness/config differences limit comparisons.",
    "Timing logs are not automatically MLflow traces. PVC/DB growth and etcd/serving metrics remain external."]


def identifier(value, model=False):
    pattern = r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*" if model else r"[A-Za-z0-9_-]{1,160}"
    return (isinstance(value, str) and len(value) <= 160
            and bool(re.fullmatch(pattern, value)) and "://" not in value
            and not re.match(r"[A-Za-z]:", value)
            and all(part not in (".", "..") for part in value.split("/")))


def finite_number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 2**63-1 and math.isfinite(value)
    except OverflowError:
        return False


def normalize(raw, issues):
    if not isinstance(raw, dict) or raw.get("event") != "span_end":
        issues["not_span_event"] += 1
        return None
    if not identifier(raw.get("span_id")) or not identifier(raw.get("trace_id")):
        issues["missing_or_unsafe_span_identity"] += 1
        return None
    event = {"event": "span_end", "additive": False}
    event["operation"] = raw.get("operation") if isinstance(raw.get("operation"), str) and raw["operation"] in OPERATIONS else "unknown.operation"
    event["layer"] = raw.get("layer") if isinstance(raw.get("layer"), str) and raw["layer"] in LAYERS else "unknown"
    event["status"] = raw.get("status") if raw.get("status") in ("success", "failed") else "unknown"
    if event["operation"] == "unknown.operation":
        issues["unknown_operation"] += 1
    for key in IDS:
        if identifier(raw.get(key)):
            event[key] = raw[key]
    for key in NUMBERS:
        value = raw.get(key)
        if finite_number(value):
            if (key in ("start_time", "end_time") and value > 32503680000) or (key == "elapsed_seconds" and value > 31536000):
                issues["numeric_value_out_of_range"] += 1
                continue
            if key not in ("start_time", "end_time", "elapsed_seconds") and not isinstance(value, int):
                issues["invalid_count"] += 1
                continue
            event[key] = value
        elif value is not None:
            issues["invalid_numeric_fields"] += 1
    for key in BOOLS:
        if isinstance(raw.get(key), bool):
            event[key] = raw[key]
    if identifier(raw.get("model"), model=True):
        event["model"] = raw["model"]
    if identifier(raw.get("error_type")):
        event["error_type"] = raw["error_type"]
    event.setdefault("run_id", "unassigned")
    event["clock_domain"] = event.get("clock_domain", "legacy_" + event["trace_id"])
    start, end, elapsed = (event.get(k) for k in ("start_time", "end_time", "elapsed_seconds"))
    event["interval_valid"] = start is not None and end is not None and end >= start
    if event["interval_valid"] and elapsed is not None and abs((end-start)-elapsed) > max(0.1, elapsed * .01):
        event["interval_valid"] = False
        issues["wall_monotonic_mismatch"] += 1
    if not event["interval_valid"]:
        issues["missing_or_invalid_wall_interval"] += 1
    if elapsed is None:
        issues["missing_monotonic_elapsed"] += 1
    if not identifier(raw.get("clock_domain")):
        issues["legacy_clock_unknown"] += 1
    return event


def read_events(paths, max_events=MAX_EVENTS, run_ids=None):
    events, issues, seen = [], Counter(), {}
    for path in paths:
        with Path(path).open("rb") as source:
            while True:
                line = source.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES:
                    issues["oversize_lines"] += 1
                    while line and not line.endswith(b"\n"):
                        line = source.readline(MAX_LINE_BYTES + 1)
                    continue
                if not line.strip():
                    continue
                if b"AUGUR_TIMING " in line:
                    line = line.split(b"AUGUR_TIMING ", 1)[1]
                elif not line.lstrip().startswith(b"{"):
                    issues["ignored_non_event_lines"] += 1
                    continue
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    issues["malformed_lines"] += 1
                    continue
                event = normalize(raw, issues)
                if event is None:
                    continue
                if run_ids and event["run_id"] not in run_ids:
                    continue
                key = (event["run_id"], event["clock_domain"], event["trace_id"], event["span_id"])
                if key in seen:
                    issues["duplicate_spans_dropped"] += 1
                    if event != seen[key]:
                        issues["conflicting_duplicate_spans"] += 1
                    continue
                if len(events) >= max_events:
                    issues["event_limit_reached"] += 1
                    return events, dict(issues)
                seen[key] = event
                events.append(event)
    return events, dict(issues)


def interval_union(intervals):
    total, start, end = 0.0, None, None
    for left, right in sorted(intervals):
        if start is None:
            start, end = left, right
        elif left <= end:
            end = max(end, right)
        else:
            total += end-start
            start, end = left, right
    return total + (end-start if start is not None else 0)


def distribution(values):
    values = sorted(v for v in values if finite_number(v))
    n = len(values)
    return {"samples": n, "min": values[0] if n else None, "max": values[-1] if n else None,
            "mean": statistics.fmean(values) if n else None,
            "p50": values[math.ceil(n*.5)-1] if n >= 5 else None,
            "p95": values[math.ceil(n*.95)-1] if n >= 20 else None,
            "p99": values[math.ceil(n*.99)-1] if n >= 100 else None}


def load_metadata(path):
    if path is None:
        return {}
    if Path(path).stat().st_size > MAX_LINE_BYTES:
        raise ValueError("metadata exceeds the size limit")
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("runs"), dict):
        raise ValueError("metadata must contain a runs object")
    result = {}
    for run_id, fields in raw["runs"].items():
        if not identifier(run_id) or not isinstance(fields, dict):
            continue
        safe = {}
        for key in METADATA:
            value = fields.get(key)
            if identifier(value, model=True) or (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
                safe[key] = value
        result[run_id] = safe
    return result


def analyze(events, issues=None, metadata=None):
    metadata = metadata or {}
    grouped = defaultdict(list)
    for event in events:
        grouped[event["run_id"]].append(event)
    runs = []
    for run_id, records in sorted(grouped.items()):
        identities = {(e["clock_domain"], e["trace_id"], e["span_id"]): e for e in records}
        clocks = defaultdict(list)
        operations = defaultdict(list)
        for e in records:
            clocks[e["clock_domain"]].append(e)
            operations[(e["operation"], e["layer"], e.get("model", "unknown"), str(e.get("stream_requested", "unknown")))].append(e)
        clock_rows = []
        for domain, spans in sorted(clocks.items()):
            valid = [e for e in spans if e["interval_valid"]]
            intervals = [(e["start_time"], e["end_time"]) for e in valid]
            parents = {(e["trace_id"], e.get("parent_span_id")) for e in spans}
            leaves = [e for e in valid if (e["trace_id"], e["span_id"]) not in parents]
            sdk = [e for e in valid if e["layer"] == "sdk"]
            io = [e for e in valid if e["layer"] == "io" and e["operation"] != "mlflow.payload_measurement"]
            left = min((i[0] for i in intervals), default=None)
            right = max((i[1] for i in intervals), default=None)
            window = right-left if left is not None else None
            occupied = interval_union(intervals)
            clock_rows.append({"clock_domain": domain, "valid_spans": len(valid), "start_time": left,
                "end_time": right, "observed_window_seconds": window, "observed_union_seconds": occupied,
                "observed_leaf_union_seconds": interval_union([(e["start_time"], e["end_time"]) for e in leaves]),
                "window_without_leaf_spans_seconds": max(0, window-interval_union([(e["start_time"], e["end_time"]) for e in leaves])) if window is not None else None,
                "sdk_interval_union_seconds": interval_union([(e["start_time"], e["end_time"]) for e in sdk]),
                "io_interval_union_seconds": interval_union([(e["start_time"], e["end_time"]) for e in io]),
                "between_observed_intervals_seconds": max(0, window-occupied) if window is not None else None})
        operation_rows = []
        for (operation, layer, model, streaming), spans in sorted(operations.items()):
            operation_rows.append({"operation": operation, "layer": layer, "model": model,
                "stream_requested": streaming, "events": len(spans),
                "failures": sum(e["status"] == "failed" for e in spans),
                "latency_seconds": distribution([e["elapsed_seconds"] for e in spans if "elapsed_seconds" in e]),
                "prompt_tokens": distribution([e["prompt_tokens"] for e in spans if "prompt_tokens" in e]),
                "output_tokens": distribution([e["output_tokens"] for e in spans if "output_tokens" in e])})
        missing = sum(bool(e.get("parent_span_id")) and (e["clock_domain"], e["trace_id"], e["parent_span_id"]) not in identities for e in records)
        outside = 0
        for e in records:
            p = identities.get((e["clock_domain"], e["trace_id"], e.get("parent_span_id")))
            if p and p["interval_valid"] and e["interval_valid"] and (e["start_time"] < p["start_time"]-.1 or e["end_time"] > p["end_time"]+.1):
                outside += 1
        done, cyclic = set(), set()
        for key in identities:
            trail, positions, current = [], {}, key
            while current in identities and current not in done:
                if current in positions:
                    cyclic.update(trail[positions[current]:])
                    break
                positions[current] = len(trail)
                trail.append(current)
                node = identities[current]
                current = (node["clock_domain"], node["trace_id"], node.get("parent_span_id"))
            done.update(trail)
        upload_rows = [{k: e[k] for k in ("clock_domain", "span_id", "status", "payload_key", "upload_ordinal",
            "mlflow_run_id", "elapsed_seconds", "payload_regular_files", "payload_logical_bytes",
            "payload_scan_complete", "payload_scan_truncated", "payload_scan_errors", "payload_symlinks") if k in e}
            for e in records if e["operation"] == "mlflow.artifact_upload"]
        run_meta = metadata.get(run_id, {})
        runs.append({"run_id": run_id, "events": len(records), "failures": sum(e["status"] == "failed" for e in records),
            "missing_parents": missing, "children_outside_parent": outside, "cyclic_parent_spans": len(cyclic), "clock_domains": clock_rows,
            "operations": operation_rows, "upload_attempts": upload_rows, "metadata": run_meta,
            "benchmark_controls_complete": CONTROLS.issubset(run_meta),
            "known_invocation_count": sum(e["operation"] in ("pipeline.run", "analysis.adhoc_invocation", "analysis.report") for e in records)})
    return {"schema_version": 1, "events_retained": len(events), "issues": issues or {}, "runs": runs,
            "limitations": LIMITATIONS, "percentile_policy": "Nearest-rank descriptive p50 needs 5, p95 needs 20, p99 needs 100 samples per operation/layer/model/stream group; no confidence claim."}


def fmt(value):
    return "unknown" if value is None else f"{value:.3f}" if isinstance(value, float) else str(value)


def render_html(summary, events):
    esc = lambda v: html.escape(fmt(v), quote=True)
    timelines = defaultdict(list)
    for event in events:
        if event["interval_valid"]:
            timelines[(event["run_id"], event["clock_domain"])].append(event)
    clock_panels = 0
    parts = ["<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Augur offline timing report</title><style>body{font:16px system-ui,sans-serif;background:#f4f7f8;color:#183440;max-width:1180px;margin:32px auto;padding:0 20px}h1,h2{line-height:1.2}details{background:white;padding:20px;margin:20px 0;border-radius:8px}summary{cursor:pointer;font-size:22px;font-weight:650}table{border-collapse:collapse;width:100%;font-size:14px;margin:16px 0}td,th{padding:8px;text-align:left;border-bottom:1px solid #d8e1e5;vertical-align:top}code{overflow-wrap:anywhere}.scroll{overflow:auto}.row{display:grid;grid-template-columns:260px 1fr;gap:12px;font-size:12px;min-height:26px}.track{background:#eef3f5;position:relative;height:19px}.bar{position:absolute;min-width:2px;height:19px;border-radius:2px;background:#568b9f}.sdk{background:#007b83}.io{background:#c2752c}.failed{outline:2px solid #b62f37}.note{color:#496474;font-size:14px}.cards{display:flex;flex-wrap:wrap;gap:16px}.card{border:1px solid #d8e1e5;padding:12px;min-width:150px}li{margin-bottom:8px}@media print{body{background:white;margin:0;max-width:none}details{break-inside:avoid}.scroll{overflow:visible}.row{grid-template-columns:200px 1fr}}</style><h1>Augur offline timing report</h1><p>Observed spans, local exports only. SDK time includes the model-service path; it does not measure GPU compute. No exact causal critical path is available.</p>"]
    parts.append(f"<p>{summary['events_retained']} retained spans. Parse/coverage issues: <code>{esc(json.dumps(summary['issues'], sort_keys=True))}</code></p>")
    if len(summary["runs"]) > MAX_HTML_RUNS:
        parts.append("<p class='note'>HTML limited to 100 runs; summary.json and CSV files retain all selected safe events.</p>")
    for run in summary["runs"][:MAX_HTML_RUNS]:
        parts.append(f"<details open><summary>Run {esc(run['run_id'])}</summary><p class='note'>{run['events']} events; {run['failures']} failed spans (nested failures can repeat the same error); {run['missing_parents']} missing parents; {run['children_outside_parent']} children outside parent; {run['cyclic_parent_spans']} cyclic parent spans. Complete benchmark controls: {esc(run['benchmark_controls_complete'])}.</p>")
        parts.append(f"<p class='note'>Approved effective controls: <code>{esc(json.dumps(run['metadata'], sort_keys=True))}</code></p>")
        for clock in run["clock_domains"]:
            if clock_panels >= MAX_HTML_CLOCKS:
                parts.append("<p class='note'>HTML clock panels limited to 100 overall; remaining domains are in summary.json and spans.csv.</p>")
                break
            clock_panels += 1
            parts.append(f"<h3>Clock domain <code>{esc(clock['clock_domain'])}</code></h3><div class='cards'>")
            for label, key in [("Observed window (s)", "observed_window_seconds"), ("Interval union (s)", "observed_union_seconds"), ("SDK union (s)", "sdk_interval_union_seconds"), ("I/O union (s)", "io_interval_union_seconds")]:
                parts.append(f"<div class='card'>{label}<br><b>{esc(clock[key])}</b></div>")
            parts.append(f"</div><p class='note'>Window without observed leaf spans: {esc(clock['window_without_leaf_spans_seconds'])} seconds; this is a coverage gap, not proven idle time. Unions can overlap each other; do not add these cards. Window is first-to-last observed valid event, not verified end-to-end run duration. Axis: seconds relative to this domain's first event. Teal: SDK; amber: I/O; blue: other; red outline: failed.</p>")
            parts.append(f"<div class='row'><span>Relative seconds</span><div style='display:flex;justify-content:space-between'><span>0</span><span>{esc(clock['observed_window_seconds'])}</span></div></div>")
            rows = sorted(timelines[(run["run_id"], clock["clock_domain"])], key=lambda e:e["start_time"])
            origin, width = clock["start_time"], max(clock["observed_window_seconds"] or 0, .001)
            for e in rows[:300]:
                left = 100*(e["start_time"]-origin)/width
                size = 100*(e["end_time"]-e["start_time"])/width
                tip = f"{e['operation']}: +{e['start_time']-origin:.3f} to +{e['end_time']-origin:.3f}s; SDK/monotonic elapsed {fmt(e.get('elapsed_seconds'))}; {e['status']}; span {e['span_id']}"
                css = ("sdk" if e["layer"] == "sdk" else "io" if e["layer"] == "io" else "") + (" failed" if e["status"] == "failed" else "")
                parts.append(f"<div class='row'><span>{esc(e['operation'])}</span><div class='track'><div class='bar {css}' style='left:{left:.5f}%;width:{size:.5f}%' title='{esc(tip)}'></div></div></div>")
            if len(rows)>300:
                parts.append("<p class='note'>Timeline truncated to 300 rows in this clock domain; full safe events remain in spans.csv.</p>")
        parts.append("<h3>Operation / model latency</h3><p class='note'>Samples stay separate by layer, model and stream mode. Percentiles below the minimum sample count are unknown. Streaming SDK spans time stream creation only.</p><div class='scroll'><table><tr><th>Operation / layer / model</th><th>Events / failed</th><th>Mean / p50 / p95 / p99 seconds</th><th>Token samples prompt / output</th></tr>")
        if len(run["operations"]) > 200:
            parts.append("<tr><td colspan='4'>HTML operation rows limited to 200 per run; full summaries remain in operations.csv and summary.json.</td></tr>")
        for op in run["operations"][:200]:
            d=op["latency_seconds"]
            parts.append(f"<tr><td>{esc(op['operation'])}<br>{esc(op['layer'])} / {esc(op['model'])}; stream={esc(op['stream_requested'])}</td><td>{op['events']} / {op['failures']}</td><td>{esc(d['mean'])} / {esc(d['p50'])} / {esc(d['p95'])} / {esc(d['p99'])}</td><td>n={op['prompt_tokens']['samples']} / {op['output_tokens']['samples']}<br>mean={esc(op['prompt_tokens']['mean'])} / {esc(op['output_tokens']['mean'])}</td></tr>")
        parts.append("</table></div><h3>Attempted upload payloads</h3><p class='note'>Logical regular-file bytes, not wire bytes or retained storage. Opaque keys/ordinals are process-local; scans may be partial and files may change after scanning.</p><div class='scroll'><table><tr><th>Payload / ordinal</th><th>MLflow run</th><th>Files / logical bytes</th><th>Complete / status / seconds</th></tr>")
        if len(run["upload_attempts"]) > 300:
            parts.append("<tr><td colspan='4'>HTML upload rows limited to 300 per run; full safe events remain in spans.csv.</td></tr>")
        for u in run["upload_attempts"][:300]:
            parts.append(f"<tr><td>{esc(u.get('payload_key'))} / {esc(u.get('upload_ordinal'))}</td><td>{esc(u.get('mlflow_run_id'))}</td><td>{esc(u.get('payload_regular_files'))} / {esc(u.get('payload_logical_bytes'))}</td><td>{esc(u.get('payload_scan_complete'))} / {esc(u.get('status'))} / {esc(u.get('elapsed_seconds'))}</td></tr>")
        parts.append("</table></div></details>")
    parts.append("<h2>Interpretation and limits</h2><ul>"+"".join(f"<li>{esc(v)}</li>" for v in LIMITATIONS)+"</ul><p class='note'>"+esc(summary["percentile_policy"])+"</p></html>")
    return "".join(parts)


def write_outputs(directory, summary, events):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory/"summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True)+"\n")
    (directory/"report.html").write_text(render_html(summary, events))
    columns = sorted(set().union(*(e.keys() for e in events))) if events else ["run_id", "span_id"]
    with (directory/"spans.csv").open("w", newline="") as dest:
        writer=csv.DictWriter(dest,fieldnames=columns);writer.writeheader();writer.writerows(events)
    rows=[]
    for run in summary["runs"]:
        for op in run["operations"]:
            rows.append({"run_id":run["run_id"],"operation":op["operation"],"layer":op["layer"],"model":op["model"],
                "stream_requested":op["stream_requested"],"events":op["events"],"failures":op["failures"],**op["latency_seconds"]})
    columns=["run_id","operation","layer","model","stream_requested","events","failures","samples","min","max","mean","p50","p95","p99"]
    with (directory/"operations.csv").open("w",newline="") as dest:
        writer=csv.DictWriter(dest,fieldnames=columns);writer.writeheader();writer.writerows(rows)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs",nargs="+",help="Local JSONL or logs with AUGUR_TIMING prefix")
    parser.add_argument("--output-dir",required=True)
    parser.add_argument("--run-id",action="append",default=[],help="Explicit run filter; repeat for comparison")
    parser.add_argument("--metadata",help="Local nonsecret effective-control manifest: {runs:{run-id:{...}}}")
    parser.add_argument("--max-events",type=int,default=MAX_EVENTS)
    args=parser.parse_args(argv)
    if not 1 <= args.max_events <= MAX_EVENTS:
        parser.error("max-events must be between 1 and 100000")
    if any(not identifier(v) for v in args.run_id):
        parser.error("run IDs must be safe identifiers")
    try:
        events,issues=read_events(args.inputs,args.max_events,set(args.run_id))
        if args.run_id:
            events=[e for e in events if e["run_id"] in args.run_id]
            missing=set(args.run_id)-{e["run_id"] for e in events}
            if missing:
                issues["requested_runs_without_events"]=len(missing)
        summary=analyze(events,issues,load_metadata(args.metadata))
        write_outputs(args.output_dir,summary,events)
    except (OSError,ValueError,RecursionError):
        parser.exit(2,"Cannot read local input/metadata or write output; check file access and JSON format.\n")
    print(f"Created offline report: {len(events)} safe spans, {len(summary['runs'])} runs. No network operations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
