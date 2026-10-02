"""Offline scenario, parser and attribution tests; Python standard library only."""
import importlib.util
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("analyze_timing", ROOT / "scripts/analyze_timing.py")
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def raw_event(**fields):
    event = dict(event="span_end", operation="openai.chat", layer="sdk", status="success",
                 span_id="s1", trace_id="t1", run_id="r1", clock_domain="c1",
                 start_time=100.0, end_time=110.0, elapsed_seconds=10.0, additive=False)
    event.update(fields)
    return event


def normalized(**fields):
    return analysis.normalize(raw_event(**fields), Counter())


class TestTimingAnalysis(unittest.TestCase):
    def test_fixture_sdk_io_parallel_serialized_unions_are_not_sums(self):
        events, issues = analysis.read_events([ROOT / "examples/timing/scenarios.jsonl"])
        summary = analysis.analyze(events, issues, analysis.load_metadata(ROOT / "examples/timing/controls.json"))
        runs = {r["run_id"]:r for r in summary["runs"]}
        self.assertEqual(runs["sdk_slow"]["clock_domains"][0]["sdk_interval_union_seconds"], 10)
        self.assertEqual(runs["io_slow"]["clock_domains"][0]["io_interval_union_seconds"], 12)
        self.assertEqual(runs["parallel"]["clock_domains"][0]["sdk_interval_union_seconds"], 6)
        self.assertEqual(runs["serialized"]["clock_domains"][0]["sdk_interval_union_seconds"], 10)
        self.assertEqual(runs["parallel"]["clock_domains"][0]["observed_union_seconds"], 7)
        self.assertEqual(runs["serialized"]["clock_domains"][0]["window_without_leaf_spans_seconds"], 1)
        self.assertEqual(issues, {})
        self.assertTrue(all(r["benchmark_controls_complete"] for r in runs.values()))
        retry = runs["retry_visible"]
        self.assertEqual(retry["failures"], 1)
        self.assertEqual(next(o for o in retry["operations"] if o["operation"] == "graphrag.retry_backoff")["events"], 1)

    def test_union_nested_overlap_adjacent_and_disjoint(self):
        self.assertEqual(analysis.interval_union([(0,10),(1,4),(3,7),(10,11),(20,22)]), 13)
        self.assertEqual(analysis.interval_union([]), 0)

    def test_clock_domains_are_never_combined_into_run_walltime(self):
        events = [normalized(span_id="a",clock_domain="c1"),normalized(span_id="b",clock_domain="c2",start_time=95,end_time=105)]
        run = analysis.analyze(events)["runs"][0]
        self.assertEqual(len(run["clock_domains"]),2)
        self.assertNotIn("walltime_seconds", run)
        self.assertTrue(all(c["observed_union_seconds"] == 10 for c in run["clock_domains"]))

    def test_clock_steps_reverse_intervals_and_missing_values_remain_unknown(self):
        issues = Counter()
        a = analysis.normalize(raw_event(end_time=120),issues)
        b = analysis.normalize(raw_event(span_id="b",end_time=90),issues)
        c = analysis.normalize(raw_event(span_id="c",start_time=None,elapsed_seconds=None),issues)
        self.assertFalse(a["interval_valid"])
        self.assertFalse(b["interval_valid"])
        self.assertFalse(c["interval_valid"])
        self.assertNotIn("elapsed_seconds",c)
        self.assertEqual(issues["wall_monotonic_mismatch"],1)
        run=analysis.analyze([a,b,c],dict(issues))["runs"][0]
        self.assertIsNone(run["clock_domains"][0]["observed_window_seconds"])

    def test_percentiles_require_sufficient_samples_and_layers_stay_separate(self):
        self.assertIsNone(analysis.distribution([1,2,3,4])["p50"])
        self.assertEqual(analysis.distribution(list(range(1,6)))["p50"],3)
        self.assertIsNone(analysis.distribution(list(range(1,20)))["p95"])
        self.assertEqual(analysis.distribution(list(range(1,21)))["p95"],19)
        self.assertIsNone(analysis.distribution(list(range(1,100)))["p99"])
        self.assertEqual(analysis.distribution(list(range(1,101)))["p99"],99)
        run=analysis.analyze([normalized(),normalized(span_id="b",layer="litellm")])["runs"][0]
        self.assertEqual(len(run["operations"]),2)

    def test_log_prefix_malformed_duplicate_conflict_and_privacy(self):
        a=raw_event(prompt="SECRET_PROMPT",headers={"Authorization":"SECRET_TOKEN"},path="/private/source")
        b={**a,"status":"failed"}
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"logs"
            source.write_text("ordinary SECRET_LINE\n{broken\nINFO:augur.timing:AUGUR_TIMING "+json.dumps(a)+"\n"+json.dumps(a)+"\n"+json.dumps(b)+"\n")
            events,issues=analysis.read_events([source])
            summary=analysis.analyze(events,issues)
            analysis.write_outputs(Path(tmp)/"report",summary,events)
            self.assertEqual(len(events),1)
            self.assertEqual(issues["malformed_lines"],1)
            self.assertEqual(issues["duplicate_spans_dropped"],2)
            self.assertEqual(issues["conflicting_duplicate_spans"],1)
            for p in (Path(tmp)/"report").iterdir():
                self.assertNotIn("SECRET",p.read_text())
                self.assertNotIn("/private/source",p.read_text())

    def test_unsafe_fields_unhashable_operations_nonfinite_and_large_numbers(self):
        issues=Counter()
        e=analysis.normalize(raw_event(operation=[],layer={},model="https://example.invalid/token",prompt_tokens=True,output_tokens=float("nan"),bytes=10**1000),issues)
        self.assertEqual(e["operation"],"unknown.operation")
        self.assertNotIn("model",e)
        self.assertNotIn("prompt_tokens",e)
        self.assertNotIn("output_tokens",e)
        self.assertNotIn("bytes",e)
        self.assertIsNone(analysis.normalize(raw_event(span_id="<script>"),issues))
        for value in ("/private/example", "../example", "namespace/../value", "C:/example/model", "C:example/model"):
            event=analysis.normalize(raw_event(model=value),issues)
            self.assertNotIn("model",event)

    def test_event_and_line_limits_and_run_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/"logs"
            source.write_text("x"*(analysis.MAX_LINE_BYTES+1)+"\n"+json.dumps(raw_event(run_id="ignore"))+"\n"+json.dumps(raw_event(run_id="selected"))+"\n"+json.dumps(raw_event(run_id="selected",span_id="b"))+"\n")
            events,issues=analysis.read_events([source],max_events=1,run_ids={"selected"})
            self.assertEqual(events[0]["run_id"],"selected")
            self.assertEqual(issues["oversize_lines"],1)
            self.assertEqual(issues["event_limit_reached"],1)

    def test_missing_cyclic_and_outside_parents_are_visible(self):
        events=[normalized(span_id="a",parent_span_id="b"),normalized(span_id="b",parent_span_id="a"),
                normalized(span_id="c",parent_span_id="missing"),normalized(span_id="d",parent_span_id="a",start_time=90,end_time=100)]
        run=analysis.analyze(events)["runs"][0]
        self.assertEqual(run["missing_parents"],1)
        self.assertEqual(run["cyclic_parent_spans"],2)
        self.assertEqual(run["children_outside_parent"],1)

    def test_legacy_clocks_separate_traces_and_unassigned_run(self):
        issues=Counter()
        a=raw_event();a.pop("clock_domain");a.pop("run_id")
        b={**a,"span_id":"b","trace_id":"t2"}
        run=analysis.analyze([analysis.normalize(a,issues),analysis.normalize(b,issues)])["runs"][0]
        self.assertEqual(run["run_id"],"unassigned")
        self.assertEqual(len(run["clock_domains"]),2)
        self.assertEqual(issues["legacy_clock_unknown"],2)

    def test_metadata_allowlist_and_partial_upload_is_not_storage_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"metadata.json"
            p.write_text(json.dumps({"runs":{"r1":{"config_id":"cfg","prompt":"SECRET","environment_id":"https://example.invalid/token"}}}))
            meta=analysis.load_metadata(p)
        self.assertEqual(meta,{"r1":{"config_id":"cfg"}})
        e=normalized(operation="mlflow.artifact_upload",layer="io",payload_logical_bytes=10,payload_scan_complete=False)
        run=analysis.analyze([e],metadata=meta)["runs"][0]
        self.assertFalse(run["benchmark_controls_complete"])
        self.assertFalse(run["upload_attempts"][0]["payload_scan_complete"])
        self.assertNotIn("persisted_storage_bytes",run)

    def test_cli_outputs_self_contained_html_and_filters_explicit_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            code=analysis.main([str(ROOT/"examples/timing/scenarios.jsonl"),"--output-dir",tmp,"--run-id","parallel","--run-id","serialized","--metadata",str(ROOT/"examples/timing/controls.json")])
            self.assertEqual(code,0)
            report=Path(tmp)/"report.html"
            text=report.read_text()
            self.assertIn("Run parallel",text)
            self.assertIn("Run serialized",text)
            self.assertNotIn("Run sdk_slow",text)
            self.assertNotIn("<script",text)
            self.assertNotIn("src=",text)
            self.assertEqual(set(p.name for p in Path(tmp).iterdir()),{"report.html","summary.json","spans.csv","operations.csv"})

    def test_extreme_aggregates_are_unknown_instead_of_overflow(self):
        issues=Counter()
        e=analysis.normalize(raw_event(elapsed_seconds=1e308),issues)
        self.assertNotIn("elapsed_seconds",e)
        self.assertEqual(issues["invalid_numeric_fields"],1)
        self.assertIsNone(analysis.distribution([1e308,1e308])["mean"])
        self.assertIsNone(analysis.analyze([e])["runs"][0]["operations"][0]["latency_seconds"]["mean"])

    def test_same_span_id_in_distinct_traces_is_not_duplicate_or_parent_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"log"
            p.write_text(json.dumps(raw_event(trace_id="t1"))+"\n"+json.dumps(raw_event(trace_id="t2"))+"\n")
            events,issues=analysis.read_events([p])
        self.assertEqual(len(events),2)
        self.assertNotIn("duplicate_spans_dropped",issues)
        events.append(normalized(span_id="child",trace_id="t3",parent_span_id="s1"))
        self.assertEqual(analysis.analyze(events)["runs"][0]["missing_parents"],1)

    def test_rejected_clock_and_unknown_stream_mode_are_visible(self):
        issues=Counter()
        a=analysis.normalize(raw_event(clock_domain="https://example.invalid/clock"),issues)
        b=normalized(span_id="b",stream_requested=False)
        self.assertEqual(issues["legacy_clock_unknown"],1)
        self.assertTrue(a["clock_domain"].startswith("legacy_"))
        rows=analysis.analyze([a,b])["runs"][0]["operations"]
        self.assertEqual({r["stream_requested"] for r in rows},{"unknown","False"})

    def test_many_runs_and_clock_panels_have_explicit_html_limits(self):
        events=[normalized(run_id=f"r{i}",span_id=f"s{i}") for i in range(102)]
        summary=analysis.analyze(events)
        text=analysis.render_html(summary,events)
        self.assertEqual(text.count("<details open>"),100)
        self.assertIn("HTML limited to 100 runs",text)
        self.assertEqual(len(summary["runs"]),102)

    def test_deeply_broken_json_is_counted_and_safe_event_still_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"log"
            p.write_text("["*2000+"0"+"]"*2000+"\n"+json.dumps(raw_event())+"\n")
            events,issues=analysis.read_events([p])
        self.assertEqual(len(events),1)
        # Non-object logs are ignored, not replayed or interpreted.
        self.assertEqual(issues["ignored_non_event_lines"],1)


if __name__ == "__main__":
    unittest.main()
