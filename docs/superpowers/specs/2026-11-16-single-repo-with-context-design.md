# Single Repo With Context Scan Design

All code paths below are relative to `workflows/examples/code_understanding/`.

## Summary

Augur gets a third run mode, named **single repo with context**. It runs the existing multi-repo scan over a list of repositories, stores a catalog of what each repository provides, then runs the deep single-repo scan on one named focal repository with its cross-repository imports resolved against that catalog.

## Why

A single-repo scan sees only the repository it is given. When that repository imports code from a sibling repository, the scan meets an unknown entity and the migration plan has to guess what it is. A multi-repo scan sees every repository but reports at overview depth. Developers need the deep report for one repository, with the repositories it depends on understood well enough to plan around them.

This mode is expected to become the default and eventually replace both existing modes. For this build it is added alongside them.

## Goals

- A third mode, selectable the same way as the existing two.
- One focal repository gets the deep, single-repo-style index and migration report.
- An import in the focal repository that is provided by a sibling repository in the list is no longer an unknown: the index and report know which repository provides it, what it does, and what it runs on.
- The multi-repo pass stores a reviewable record (the repo catalog) of what each repository provides.
- The multi-repo overview report is still produced.

## Non-Goals

- Discovering related repositories automatically. The user supplies the list.
- Deep-scanning every repository in the list.
- Planning the migration of sibling repositories. They are described, not planned.
- Resolving transitively (what a sibling's own imports resolve to).
- Resolving declared build dependencies (for example a sibling's artifact in `pom.xml`) to sibling repositories. Only imports are resolved in this build. A sibling may therefore also remain in the focal graph as an ordinary `library` entry; the sample run must record whether that muddies the report, which decides whether dependency matching follows.
- Removing or changing the existing single-repo and multi-repo modes.
- Pulling sibling source code into the focal index.

## Anti-Goals

- A focal report diluted with sibling content, losing the single-repo focus.
- Any change in output of the existing two modes.
- New LLM calls to build the catalog or annotate the focal repository.
- Re-running LLM data generation for the focal repository in the deep pass.

## Inputs and Selection

- Repository list: obtained exactly as multi-repo mode obtains it today in each variant. Local: `GIT_REPO_LIST_CONTENTS`, else `repos/repo_list.json` from the asset loader. Kubeflow: `GIT_REPO_LIST_CONTENTS` only (`get_repo_list_op` has no fallback). This mode does not change that.
- Focal repository: `GIT_REPO` and `GIT_BRANCH`, as in single-repo mode.
- The focal repository must be in the list, compared by slug (`generate_slug_from_repo`). If it is not, or `GIT_REPO` is empty, the run fails before any work starts.
- `scripts/run_pipelines.sh` gains `--single-repo-with-context`, triggering a pipeline registered as `single_repo_with_context`. The existing default (multi) is unchanged. The Makefile already passes `ARGS` through.
- `pipelines/orchestrator.py` gains `single_repo_with_context_pipeline` (KFP name `single-repo-with-context-pipeline`), added to the compile map.

## Flow

1. **Overview pass.** The existing multi-repo pipeline runs unchanged: data generation per repository, combined index, overview report.
2. **Catalog.** After data generation for all repositories, build the repo catalog from the generated output and store it.
3. **Annotate.** Rewrite the focal repository's generated file headers: mark it single-repo and annotate each import that resolves to a sibling.
4. **Deep pass.** Run the existing single-repo indexing (including evaluation) and analysis on the annotated focal output, with `multi_repo=False`.

Steps 2 to 4 depend only on data generation, not on the overview index or report, so under Kubeflow they may run alongside the overview indexing.

Both the local (`pipelines/base`) and Kubeflow (`pipelines/kubeflow`) variants must work, following the existing pattern where a Kubeflow op wraps the base implementation.

## The Repo Catalog

### Source

Data generation already writes, per source file, a `<file>_metadata.txt` (YAML from `flatten_code_metadata`) next to the header-annotated `<file>.txt`, under `<parent_target>/<slug>/`, and uploads the directory to `results/metadata/multi-repo/<slug>`. `download_code_metadata_directories` fetches these for every repository. The catalog is a deterministic rollup of two things already in each repository's generated output:

- the `_metadata.txt` files, for packages, files, purposes, classes, and imports;
- the repository's `provenance.json` (written at clone time, see `provenance/pipeline.py`), for runtime and library versions. Runtime is never in `_metadata.txt`: the header's runtime suffix comes from the repository's optional `.code_metadata/` directory and is not saved to the metadata file.

Data generation itself does not change. `flatten_code_metadata` fills empty schema placeholders (for example `runtime_stack_0_name: ''`); the rollup treats empty values as absent.

### Contents

One JSON document, one entry per repository:

- `git_repo`, `git_branch`, `git_slug`
- `languages`
- `runtime_stack`: name and version entries, from `provenance.json`, falling back to the `, runtime ...` suffix of the generated file headers
- `libraries`: name and version, from `provenance.json`
- `packages`: for each distinct `package` value, the files in it, each with `file_path`, `purpose`, and `classes`

Fields a repository's metadata lacks are omitted, not invented.

### Storage

Written as `repo_catalog.json` at `<parent_target>/` and logged through `DefaultAssetLoader().log_results` under the multi-repo metadata path, tagged `category: data-generation`, `multi_repo: True`, `repo-catalog: true`. Multi-repo indexing copies all of `<parent_target>` into its GraphRAG `input/`, so `repo_catalog.json` is added to the list of files that copy removes (the telemetry exclusion list in `pipelines/base/indexing.py`).

## Import Resolution

For each file in the focal repository, each entry in its `imports`:

- It resolves to a sibling when a sibling package name equals the import, or is a prefix of it ending at a separator (`.`, `/`, `::`). Example: package `timetable.api` provides `timetable.api.routes`.
- The longest matching package wins.
- The focal repository's own packages never match; those are ordinary internal imports.
- If two siblings match equally, annotate with both.
- Relative imports (leading dots) are never resolved.
- No match leaves the import line exactly as it is today.

When the import also identifies a specific file in the sibling package, use that file's purpose; otherwise use the package's files' purposes, shortened to one line.

### Header annotation

A resolved import line in the focal file header changes from:

```
- [import] timetable.api.routes
```

to one line of the form:

```
- [import] timetable.api.routes (provided by repository https://github.com/org/timetable, slug timetable-main; purpose: <one line>; runtime: Python 3.12)
```

The header's first line is forced to `multi_repo false`, whatever it said before (the flag there is echoed by the LLM and is not reliably `true`), so the focal index is built as a single-repo index. Nothing else in the `.txt` file changes.

The focal `_metadata.txt` files are indexed as documents too, so they get the same treatment in the annotated copy: `multi_repo` forced to `false` and each resolved `imports` value carrying the same annotation. Otherwise half the focal corpus would still read as multi-repo with unresolved imports.

The annotated focal output is a separate copy; the multi-repo copy used by the overview index is left untouched.

### How a sibling appears in the focal graph

A resolved import becomes a `module` entity named `<sibling-slug>::<import name>`, with the annotation's purpose and runtime in its description, linked from the importing module by the existing `CROSS_REPO_IMPORTS` relationship. This is the vocabulary multi-repo indexes already use.

It is not a `repository` entity: `extract_graph.txt` forbids emitting one, `settings.yaml.in` has no such entity type, and `provenance/graph.py` (`ground_repository`) would merge it into the focal repository. Those stay as they are.

`extract_graph.txt` gets one added rule that applies only to import lines carrying a `provided by repository` annotation. No existing mode produces such a line, so their extraction is unaffected. The builder must confirm that a `<sibling-slug>::` module in a `multi_repo false` index survives the provenance grounding and stitch steps without being rewritten or dropped; if it does not, the fallback is to leave the sibling typed as `library` with the enriched description, with no provenance changes.

## Results Storage

The deep pass stores its index, report, and telemetry on the normal single-repo paths (`results/.../<focal-slug>`), so ad-hoc queries and anything else that reads a single-repo index work with no changes. A later plain single-repo run of the same repository overwrites it, and vice versa. Deep-pass artifacts carry an extra tag `scan_mode: single-repo-with-context`.

## Failure Behavior

- Focal repository missing from the list, or `GIT_REPO` empty: fail before starting.
- Any repository in the list with no generated data available: the run fails, exactly as multi-repo indexing does today (`download_code_metadata_directories` raises). The deep pass does not try to continue past it.
- An unreadable `_metadata.txt` or a missing `provenance.json` is skipped with a warning.

Known limitation, inherited unchanged from multi-repo mode: downloads take the latest matching MLflow run, so a repository whose data generation failed in this run but succeeded in an earlier one is served from the earlier run without notice. That applies to the focal repository too.

## Approaches Considered

- **A. Resolve in the file headers (chosen).** Annotate focal imports before indexing. The knowledge lands in the index, so every report section and ad-hoc query sees it. Cost: import-to-package matching across languages.
- **B. Feed the catalog to the report prompts.** Least code, but the index still treats the imports as unknown, ad-hoc queries gain nothing, and prompt context grows with the list.
- **C. Index sibling code with the focal repository.** Real code instead of a summary, but a larger index and a report that loses its single-repo focus.

A was picked by the project owner because it fixes the unknown where it originates and reuses the existing header mechanism (`generate_code_comment`).

## Testing

- Unit tests for the catalog rollup and for import resolution: exact match, prefix match, longest match, own-package exclusion, tie, relative import, no match.
- A test that header annotation changes only the first line's flag and the resolved import lines.
- A test that the existing modes' generated headers are byte-identical before and after this change.
- A manual run against the two sample repositories in `assets/repos/repo_list1.json` with `northpolesouthern-timetable` as focal (it imports `com.northpolesouthern.train.*`), confirming the focal report describes the sibling instead of an unknown. Do this early: how reliably LLM-extracted `package` and `imports` values line up across repositories is the main risk in this design.

## Left to the Builder

- How the focal header is rewritten without an LLM call (patching the existing header text, or re-rendering it from `_metadata.txt` through `generate_code_comment`).
- Where the catalog and annotation code lives, and how the Kubeflow ops are split.
- The exact wording of the annotation, provided it stays on one line and carries repository URL, slug, purpose, and runtime.
- The wording of the added `extract_graph.txt` rule.
- How deep-pass token and duration telemetry combines with the overview pass's; best effort.

## Decisions Made by the Designer, Not the Owner

These were not discussed and are open to correction:

- The overview pass always runs in this mode.
- Deep-pass results share the single-repo storage paths.
- The `scan_mode` tag name and the `repo_catalog.json` name and location.
- Any repository with no generated data fails the run, matching multi-repo mode.
- A sibling appears in the focal graph as a slug-prefixed `module`, not a `repository` entity.
- The focal `_metadata.txt` files are annotated along with the headers.
- Runtime and library versions come from `provenance.json`.
- No report prompt changes; the annotations reach the report through the index.
- Flag and pipeline names (`--single-repo-with-context`, `single_repo_with_context`).
