# AUGUR: Code Understanding Workflow

This directory contains the core pipelines and components for AUGUR's Code Understanding workflow:
- **`pipelines/base/`**: Base Python implementations of the three pipeline stages:
  1. `data_generation.py` (Source ingestion, language detection, LLM code & config metadata extraction)
  2. `indexing.py` (GraphRAG knowledge graph extraction and vector embedding generation)
  3. `analysis.py` (Dependency extraction, architectural analysis, and migration report generation)
- **`pipelines/kubeflow/`**: Kubeflow Pipelines (KFP) wrappers for containerized, distributed execution across Kubernetes pods.
- **`pipelines/graphrag.py`**: In-process GraphRAG execution runner replacing legacy shell scripts.
- **`utils/`**: Core utilities including:
  - [`token_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/token_tracker.py): Real-time token usage and cost estimation singleton.
  - [`duration_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/duration_tracker.py): Per-step pipeline duration measurement and latency profiling.
  - [`graphrag_utils.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/graphrag_utils.py): GraphRAG querying, dependency analysis, and markdown report synthesis.
- **`telemetry/`**: Pluggable telemetry abstraction layer (`CustomTelemetry`, `MlFlowCustomTelemetry`, `BasicCustomTelemetry`).
- **`loaders/`**: Pluggable asset loaders for local filesystem and MLflow artifact registries.
- **`eval/`**: Benchmark evaluation and LLM-as-judge scoring.
- **`tests/`**: Comprehensive unit tests for telemetry, token cost tracking, durations, and pipeline execution.

---

## Maintainer Guides

## Application migration analysis

The prompts under `assets/prompts/analysis` assess application migration and
recommend modernization from each application's detected libraries, frameworks,
runtimes, build tooling, and integrations. They prescribe no fixed destination
stack. Recommendations can retain, upgrade, replace, or remove dependencies;
each action must explain its application benefit, compatibility prerequisites,
affected components, and validation requirements.

The report first establishes a dependency inventory and then generates security
and support findings, modernization recommendations, a component migration plan,
integration findings, and regression risks. The enhanced prompts receive all of
those findings because they run without querying the index. The end-to-end plan
also receives the characterization test plan and the enhanced component plan.
Source/version gaps remain explicit, and generated findings do not become
independent proof of compatibility or security claims.

Both single-repository and multi-repository reports include all twelve report
sections. Multiple repositories retain separate dependency versions and
recommendations; coordinated migration requires evidence of a dependency or
integration. Enabling the previously skipped sections increases the number of
LLM queries for multi-repository reports from three to twelve and supplies more
context to later queries.

Exact target versions, CVEs, supported upgrade paths, and support-status claims
require supplied reference evidence. Without it, the prompts recommend a
direction, mark verification tasks, and use `null` for unknown JSON scalar
values. Imported package names do not establish versions, declared constraints
are distinguished from resolved versions, and conflicting versions retain their
repository/module scope. This workflow does not automatically fetch vendor
documentation or perform a live vulnerability scan.

### Output and asset changes

Consumers of the earlier RHEL-oriented report must migrate these JSON fields:

| Previous field | Application migration field |
| --- | --- |
| `rhel_enhanced_code_migration_plan` | `modernization_enhanced_code_migration_plan` |
| `rhel10_compatibility_issues` | `application_compatibility_issues` (array of strings) |
| `rhel10_compatibility_issues_reference_sources` | `application_compatibility_issues_reference_sources` |

The `code_migration_plan` and `end_to_end_migration_plan` top-level keys and
existing general plan fields remain. Component plans add modernization actions,
prerequisites, and validation steps. Enhanced component plans include structured
`modernization_recommendations` with current/target versions, action, rationale,
evidence status, sources, and verification tasks. The end-to-end plan adds
affected repositories/components, completion criteria, and rollback
considerations. `runtime_upgrade_required` now describes an application-driven
change or is `null` when no change is established. Unknown versions remain
`null`; absent lists are `[]`.

The static assets `analysis/system-prompt/rhel-admin` and
`analysis/additional-context/rhel8-to-10` are replaced by
`analysis/system-prompt/application-migration` and
`analysis/additional-context/application-modernization`. Deployments using
`ASSET_LOADER=mlflow` must register the updated assets together with the analyzer
update using their existing asset publication process. No assets are published
automatically by this change.

## Repository exclusions with `.augurignore`

To exclude repository content from Augur analysis, commit an optional
`.augurignore` file at the root of the repository before running Augur. Augur
reads only `<repository-root>/.augurignore` from the selected remote branch;
nested files and runtime patterns are unsupported.

The file uses Git-compatible ignore rules through `pathspec`, including ordered
rules, wildcards, directory rules, and negation. Its exclusions are additive to
Augur's built-in exclusions: a negated rule cannot re-include a file or
directory that Augur already excludes. If the file is absent, empty, or
comment-only, existing behavior is unchanged.

Augur must clone the repository before it can discover this file, so its rules
do not reduce clone or download contents. Once loaded, ignored paths are
skipped during language detection, metadata loading, and dataset generation.
A present file that cannot be read as UTF-8 or compiled fails that repository
before analysis rather than proceeding without the requested exclusions.

Normal logs report whether the root file was found, its active-pattern count,
the active patterns in file order, and aggregate excluded file and directory
counts. Individual excluded paths are logged only at debug level.

```gitignore
# Generated fixtures
tests/fixtures/generated/

# Large captured payloads
**/*.har

# Ignore reports except the maintained example
reports/*
!reports/example.md
```

## Telemetry & Metrics Maintenance Guide

- 👉 **[README_LOGGING.md](../../../README_LOGGING.md)**: Unified console logging, `LOGLEVEL` environment configuration, and container stdout architecture.
- 👉 **[README_METRICS.md](../../../README_METRICS.md)**: Telemetry architecture, LLM token metrics, pipeline duration tracking, and metric expansion guide.
