"""Command line entry point for the provenance stages.

Four subcommands match the four points at which this package touches the
pipeline. ``extract`` runs before indexing and writes the ground-truth record.
``health`` enriches that record with upstream status. ``stitch`` runs after
indexing and writes the deterministic facts into the graph. ``verify`` then
checks what came out against what went in.

``vulns`` joins the facts record's declared constraints against the
affected ranges in Grype's local database, and needs no index at all.
``advisories`` writes that database's Python ranges as a JSON snapshot, and
optionally publishes it, for the analysis stage, which has no grype.
``eol-table`` regenerates the dated lifecycle table from endoflife.date.

``relevant-files`` checks the paths a migration plan's JSON claims against
the indexed documents and the checkout they came from.

``compare`` also sits outside that sequence. It verifies two
indexes built from the same checkout and reports how the metrics moved between
them, which is the shape of an ingest experiment rather than a stage.

Each stage reads and writes files rather than holding state in memory, so a run
interrupted after extraction resumes at health or verification without
repeating the work, and so the facts file can be inspected by hand.

    python -m provenance extract /path/to/repo -o facts/celery.json
    python -m provenance health facts/celery.json --cache .veripak-cache
    python -m provenance stitch facts/celery.json output/
    python -m provenance verify facts/celery.json output/
    python -m provenance vulns facts/celery.json
    python -m provenance advisories advisories-python.json --publish
    python -m provenance eol-table
    python -m provenance relevant-files output/ /path/to/repo answer.md
    python -m provenance compare facts/celery.json positional/out parser/out
    python -m provenance compare --arm purpose facts/celery.json parser/out purpose/out
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import arms as arms_module
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


def cmd_vulns(args: argparse.Namespace) -> int:
    from . import vulns

    collected = facts_module.read(Path(args.facts))
    try:
        database = (
            vulns.from_snapshot(json.loads(Path(args.snapshot).read_text(encoding="utf-8")))
            if args.snapshot else vulns.load_database()
        )
    except vulns.GrypeNotAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    findings = vulns.assess_all(collected.packages, database)
    if args.json:
        payload = {
            "db_built": database.built,
            "db_schema": database.schema,
            "findings": [f.to_dict() for f in findings],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(vulns.render(findings, database))
    # A declared range admitting a known-vulnerable version is a result, but
    # a runner that reads only the exit status should still see it.
    affected = any(f.verdict in vulns.AFFECTED for f in findings)
    return EXIT_FINDINGS if affected else EXIT_OK


def cmd_advisories(args: argparse.Namespace) -> int:
    import os

    from . import vulns

    output = Path(args.output)
    if args.publish and output.name != vulns.SNAPSHOT_FILENAME:
        print(f"error: publish a file named {vulns.SNAPSHOT_FILENAME}; the analysis "
              "stage looks for it by name", file=sys.stderr)
        return EXIT_ERROR
    if args.publish and os.getenv("ASSET_LOADER") != "mlflow":
        print("error: --publish needs ASSET_LOADER=mlflow and MLflow credentials; the local "
              f"loader publishes nothing. Point {vulns.SNAPSHOT_ENV} at the file instead.",
              file=sys.stderr)
        return EXIT_ERROR
    try:
        database = vulns.load_database()
    except vulns.GrypeNotAvailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    output.write_text(json.dumps(vulns.to_snapshot(database)), encoding="utf-8")
    ranges = sum(len(r) for r in database.ranges.values())
    print(f"{output}: {len(database.ranges)} packages, {ranges} ranges, built {database.built}")
    if not args.publish:
        return EXIT_OK
    from loaders.default_asset_loader import DefaultAssetLoader

    DefaultAssetLoader().log_results(str(output), artifact_path=vulns.SNAPSHOT_ARTIFACT_DIR,
                                     tags={**vulns.SNAPSHOT_TAGS, "db_built": database.built})
    print(f"published to {vulns.SNAPSHOT_ARTIFACT_DIR}/{vulns.SNAPSHOT_FILENAME}")
    return EXIT_OK


def cmd_eol_table(args: argparse.Namespace) -> int:
    import datetime as dt

    from . import eol

    table = eol.fetch(dt.date.today())
    output = Path(args.output) if args.output else eol.TABLE_PATH
    output.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    cycles = sum(len(p["cycles"]) for p in table["products"].values())
    print(f"{output}: {len(table['products'])} products, {cycles} cycles, "
          f"retrieved {table['retrieved']}")
    return EXIT_OK


def cmd_relevant_files(args: argparse.Namespace) -> int:
    import pandas as pd

    from . import relevant_files
    from .corpus import from_documents

    documents = Path(args.output_dir) / "documents.parquet"
    if not documents.is_file():
        raise FileNotFoundError(f"{documents} does not exist; is this a GraphRAG output directory?")
    try:
        evidence = relevant_files.Evidence.from_checkout(
            from_documents(pd.read_parquet(documents)), Path(args.checkout)
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    answers = [
        relevant_files.check(Path(a).name, Path(a).read_text(), evidence) for a in args.answers
    ]
    if args.json:
        print(json.dumps(relevant_files.to_dict(answers, evidence), indent=2))
    else:
        print(relevant_files.render(answers, evidence))
    return EXIT_OK if relevant_files.clean(answers) else EXIT_FINDINGS


def cmd_compare(args: argparse.Namespace) -> int:
    arm = arms_module.ARMS[args.arm]
    labels = tuple(args.labels) if args.labels else arm.labels
    collected = facts_module.read(Path(args.facts))
    comparison = compare_module.compare(
        verify_index(collected, Path(args.baseline_dir)),
        verify_index(collected, Path(args.treatment_dir)),
        labels=labels,
        predictions=arm.predictions,
    )
    if args.json:
        print(json.dumps(comparison.to_dict(), indent=2))
    else:
        print(compare_module.render(comparison, experiment=args.experiment or arm.experiment))
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

    vulns = sub.add_parser(
        "vulns", help="join declared constraints against grype's advisory ranges"
    )
    vulns.add_argument("facts", help="facts JSON written by extract")
    vulns.add_argument("--snapshot", help="join against this snapshot instead of grype")
    vulns.add_argument("--json", action="store_true")
    vulns.set_defaults(func=cmd_vulns)

    advisories = sub.add_parser(
        "advisories", help="write grype's Python advisory ranges as a JSON snapshot"
    )
    advisories.add_argument("output", help="where to write the snapshot")
    advisories.add_argument(
        "--publish", action="store_true", help="log it to MLflow for the analysis stage"
    )
    advisories.set_defaults(func=cmd_advisories)

    eol_table = sub.add_parser(
        "eol-table", help="regenerate the lifecycle table from endoflife.date"
    )
    eol_table.add_argument("-o", "--output", default=None,
                           help="where to write it (default: the committed asset)")
    eol_table.set_defaults(func=cmd_eol_table)

    relevant = sub.add_parser(
        "relevant-files",
        help="check a migration plan's relevant_files against the index and checkout",
    )
    relevant.add_argument("output_dir", help="GraphRAG output directory holding documents.parquet")
    relevant.add_argument("checkout", help="the repository checkout the index was built from")
    relevant.add_argument("answers", nargs="+", help="answer text files, one plan each")
    relevant.add_argument("--json", action="store_true")
    relevant.set_defaults(func=cmd_relevant_files)

    compare = sub.add_parser(
        "compare",
        help="verify two indexes of the same checkout and report how the metrics moved",
    )
    compare.add_argument("facts", help="facts JSON written by extract, shared by both indexes")
    compare.add_argument("baseline_dir", help="GraphRAG output directory for the control")
    compare.add_argument("treatment_dir", help="GraphRAG output directory for the change")
    compare.add_argument(
        "--arm",
        choices=sorted(arms_module.ARMS),
        default=arms_module.HEADER.name,
        help="which registered experiment's predictions to grade against",
    )
    compare.add_argument(
        "--labels",
        nargs=2,
        metavar=("BASELINE", "TREATMENT"),
        help="what the two indexes are called in the report (default: the arm's)",
    )
    compare.add_argument(
        "--experiment",
        help="what was varied, printed above the metrics (default: the arm's)",
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
