"""Command line entry point for the provenance stages.

Four subcommands match the four points at which this package touches the
pipeline. ``extract`` runs before indexing and writes the ground-truth record.
``health`` enriches that record with upstream status. ``stitch`` runs after
indexing and writes the deterministic facts into the graph. ``verify`` then
checks what came out against what went in.

``compare`` is the fifth and sits outside that sequence. It verifies two
indexes built from the same checkout and reports how the metrics moved between
them, which is the shape of an ingest experiment rather than a stage.

Each stage reads and writes files rather than holding state in memory, so a run
interrupted after extraction resumes at health or verification without
repeating the work, and so the facts file can be inspected by hand.

    python -m provenance extract /path/to/repo -o facts/celery.json
    python -m provenance health facts/celery.json --cache .veripak-cache
    python -m provenance stitch facts/celery.json output/
    python -m provenance verify facts/celery.json output/
    python -m provenance compare facts/celery.json positional/out parser/out
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import compare as compare_module
from . import facts as facts_module
from .models import Layer
from .sbom import SyftNotAvailable, syft_version
from .verify import render, verify_index

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _summarize(facts) -> str:
    by_layer = {
        layer.value: len(facts.packages_for_layer(layer))
        for layer in Layer
        if facts.packages_for_layer(layer)
    }
    distinct = len({p.key for p in facts.packages})
    pinned = sum(1 for p in facts.packages if p.version)
    constrained = sum(1 for p in facts.packages if p.constraint)
    lines = [
        f"root:        {facts.root}",
        f"git_repo:    {facts.git_repo or '(none)'}",
        f"git_slug:    {facts.git_slug or '(none)'}",
        f"git_commit:  {facts.git_commit or '(none)'}",
        f"own modules: {', '.join(facts.own_modules) or '(none)'}",
        f"packages:    {len(facts.packages)} declarations, {distinct} distinct",
        f"             {constrained} with a constraint, {pinned} resolving to one version",
        f"by layer:    {by_layer or '(none)'}",
        f"base images: {len(facts.base_images)}",
    ]
    for image in facts.base_images:
        lines.append(f"  {image.reference}  ({image.source_file}:{image.line_number})")
    if facts.warnings:
        lines.append(f"warnings:    {len(facts.warnings)}")
        lines.extend(f"  {w}" for w in facts.warnings)
    return "\n".join(lines)


def cmd_extract(args: argparse.Namespace) -> int:
    if args.syft:
        try:
            print(f"syft {syft_version()}", file=sys.stderr)
        except SyftNotAvailable as exc:
            print(f"warning: {exc}", file=sys.stderr)

    collected = facts_module.collect(
        args.root,
        multi_repo=args.multi_repo,
        sbom_path=Path(args.sbom) if args.sbom else None,
        run_syft=args.syft,
        force=args.force,
    )
    if args.output:
        path = facts_module.write(collected, Path(args.output))
        print(f"wrote {path}", file=sys.stderr)
    if args.json:
        print(json.dumps(collected.to_dict(), indent=2, sort_keys=True))
    else:
        print(_summarize(collected))
    return EXIT_OK


def cmd_health(args: argparse.Namespace) -> int:
    from .health import enrich, veripak_available

    if not veripak_available():
        print(
            "veripak not found on PATH. Install it with 'pip install veripak'.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    collected = facts_module.read(Path(args.facts))
    results = enrich(
        collected.packages_for_layer(Layer.PACKAGE),
        cache_dir=Path(args.cache),
        skip_cves=args.no_cves,
        limit=args.limit,
        pause_seconds=args.pause,
        force=args.force,
    )
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    print(json.dumps(results, indent=2, sort_keys=True))
    return EXIT_OK


def cmd_stitch(args: argparse.Namespace) -> int:
    from .stitch import apply as stitch_apply
    from .stitch import render as render_stitch

    collected = facts_module.read(Path(args.facts))
    report = stitch_apply(
        collected,
        Path(args.output_dir),
        backup=args.backup,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render_stitch(report, limit=args.limit))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    collected = facts_module.read(Path(args.facts))
    report = verify_index(collected, Path(args.output_dir))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(render(report, limit=args.limit))
    # A non-clean index is a result, not a crash, but it has to be visible to a
    # pipeline runner that only looks at the exit status.
    return EXIT_OK if report.ok else EXIT_FINDINGS


def cmd_compare(args: argparse.Namespace) -> int:
    collected = facts_module.read(Path(args.facts))
    comparison = compare_module.compare(
        verify_index(collected, Path(args.baseline_dir)),
        verify_index(collected, Path(args.treatment_dir)),
        labels=(args.labels[0], args.labels[1]),
    )
    if args.json:
        print(json.dumps(comparison.to_dict(), indent=2))
    else:
        print(compare_module.render(comparison, experiment=args.experiment))
    # A broken prediction is the informative outcome rather than a failure, but
    # it has to reach a runner that only reads the exit status, and a run whose
    # predictions all held is the one worth looking at twice.
    return EXIT_FINDINGS if comparison.broken_predictions else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provenance",
        description="Deterministic facts and post-index verification for AUGUR.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log progress to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="collect ground truth from a checkout")
    extract.add_argument("root", help="repository checkout to analyse")
    extract.add_argument("-o", "--output", help="write the facts JSON here")
    extract.add_argument("--sbom", help="write or reuse the raw syft document here")
    extract.add_argument("--multi-repo", action="store_true")
    extract.add_argument(
        "--no-syft", dest="syft", action="store_false", help="skip the SBOM scan"
    )
    extract.add_argument("--force", action="store_true", help="ignore cached SBOM output")
    extract.add_argument("--json", action="store_true", help="print the full record")
    extract.set_defaults(func=cmd_extract, syft=True)

    health = sub.add_parser("health", help="enrich the package layer with veripak")
    health.add_argument("facts", help="facts JSON written by extract")
    health.add_argument("--cache", default=".veripak-cache", help="per-package result cache")
    health.add_argument("-o", "--output", help="write the health JSON here")
    health.add_argument("--no-cves", action="store_true", help="skip CVE lookup")
    health.add_argument("--limit", type=int, help="audit only the first N distributions")
    health.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds between packages, to stay under registry rate limits",
    )
    health.add_argument("--force", action="store_true", help="ignore the cache")
    health.set_defaults(func=cmd_health)

    stitch = sub.add_parser(
        "stitch", help="write deterministic facts into a finished index"
    )
    stitch.add_argument("facts", help="facts JSON written by extract")
    stitch.add_argument("output_dir", help="GraphRAG output directory holding entities.parquet")
    stitch.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        help="do not keep the pre-stitch table as entities.parquet.orig",
    )
    stitch.add_argument(
        "--dry-run", action="store_true", help="report what would change and write nothing"
    )
    stitch.add_argument("--json", action="store_true")
    stitch.add_argument("--limit", type=int, default=20, help="changes shown")
    stitch.set_defaults(func=cmd_stitch, backup=True)

    verify = sub.add_parser("verify", help="check a finished index against the facts")
    verify.add_argument("facts", help="facts JSON written by extract")
    verify.add_argument("output_dir", help="GraphRAG output directory holding entities.parquet")
    verify.add_argument("--json", action="store_true")
    verify.add_argument("--limit", type=int, default=20, help="findings shown per category")
    verify.set_defaults(func=cmd_verify)

    compare = sub.add_parser(
        "compare",
        help="verify two indexes of the same checkout and report how the metrics moved",
    )
    compare.add_argument("facts", help="facts JSON written by extract, shared by both indexes")
    compare.add_argument("baseline_dir", help="GraphRAG output directory for the control")
    compare.add_argument("treatment_dir", help="GraphRAG output directory for the change")
    compare.add_argument(
        "--labels",
        nargs=2,
        metavar=("BASELINE", "TREATMENT"),
        default=["positional", "parser"],
        help="what the two indexes are called in the report",
    )
    compare.add_argument(
        "--experiment",
        default=compare_module.EXPERIMENT,
        help="what was varied, printed above the metrics so the run is attributable",
    )
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(func=cmd_compare)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
