# AUGUR Code Metrics Telemetry: CodeMetricsTracker Architecture & Reference Guide
*Comprehensive guide for the codebase scope, file counting, and line-of-code telemetry subsystem.*

---

## Table of Contents
1. [Overview & Purpose](#1-overview--purpose)
2. [Core Architecture & Design Patterns](#2-core-architecture--design-patterns)
   - [Singleton Pattern](#singleton-pattern)
   - [Memory & Resource Protection (Oversized File Handling)](#memory--resource-protection-oversized-file-handling)
   - [Directory Pruning & Exclusion Rules](#directory-pruning--exclusion-rules)
3. [Language & Syntax Engine](#3-language--syntax-engine)
   - [SLOC vs. Comment vs. Blank Classification](#sloc-vs-comment-vs-blank-classification)
   - [Supported Languages & Extension Matrix](#supported-languages--extension-matrix)
4. [Class API Reference](#4-class-api-reference)
   - [Lifecycle & Initialization](#lifecycle--initialization)
   - [Measurement & Parsing Methods](#measurement--parsing-methods)
   - [Serialization & Cross-Stage Persistence](#serialization--cross-stage-persistence)
   - [MLflow Integration](#mlflow-integration)
   - [Formatting & Visual Presentation](#formatting--visual-presentation)
   - [Extraction Helpers](#extraction-helpers)
5. [End-to-End Pipeline Integration Walkthrough](#5-end-to-end-pipeline-integration-walkthrough)
   - [Stage 1: Data Generation](#stage-1-data-generation)
   - [Stage 2: GraphRAG Indexing](#stage-2-graphrag-indexing)
   - [Stage 3: Analysis & Report Generation](#stage-3-analysis--report-generation)
6. [Artifact Formats & Presentation Outputs](#6-artifact-formats--presentation-outputs)
   - [`project_metrics.json` Schema](#project_metricsjson-schema)
   - [Console ASCII Summary Table](#console-ascii-summary-table)
   - [Markdown Report Section (`migration_report.md`)](#markdown-report-section-migration_reportmd)
   - [MLflow Scalar Metrics](#mlflow-scalar-metrics)
7. [Unit Testing & Verification](#7-unit-testing--verification)

---

## 1. Overview & Purpose

In AI-driven code modernization and migration pipelines like **AUGUR**, understanding the precise scope and composition of a repository is essential. Modernization recommendations, LLM chunking budgets, GraphRAG indexing overhead, and migration timelines directly correlate with:
- **How many files** exist in the repository (distinguishing pure source files from build/deployment configs).
- **How many lines of code (SLOC)** are being analyzed versus comment documentation and blank lines.
- **The language distribution** across the target codebase.

The [`CodeMetricsTracker`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/code_metrics_tracker.py) class provides a centralized, deterministic, and distributed telemetry mechanism that measures repository size at the earliest ingestion stage (**Data Generation**), propagates those measurements through **Indexing**, and embeds structured scope summaries into the final modernization report (**Analysis**) alongside token costs and duration metrics.

```
       +--------------------------------------------------------------------------+
       |                         AUGUR Pipeline Workflow                          |
       +--------------------------------------------------------------------------+
                                          |
            1. DATA GENERATION            v
       +--------------------------------------------------------------------------+
       | - Clones repository & detects languages                                  |
       | - CodeMetricsTracker.measure_repository(source_dir)                     |
       | - Saves project_metrics.json to tmp_target & uploads to MLflow           |
       | - Logs ASCII Code Metrics Summary to pod stdout                          |
       +--------------------------------------------------------------------------+
                                          | (KFP Dataset Artifact / MLflow Sync)
            2. GRAPHRAG INDEXING          v
       +--------------------------------------------------------------------------+
       | - Loads project_metrics.json from upstream stage                         |
       | - Preserves project_metrics.json in GraphRAG output directory            |
       | - Uploads project_metrics.json to MLflow indexing run                    |
       | - Logs ASCII Code Metrics Summary to pod stdout                          |
       +--------------------------------------------------------------------------+
                                          | (KFP Dataset Artifact / MLflow Sync)
            3. ANALYSIS & REPORT          v
       +--------------------------------------------------------------------------+
       | - DependencyAnalyzer loads project_metrics.json                          |
       | - Formats Markdown table: "### Project Codebase & Scope Summary"         |
       | - Injects scope table directly into migration_report.md                  |
       | - Logs final MLflow metrics & prints summary table                       |
       +--------------------------------------------------------------------------+
```

---

## 2. Core Architecture & Design Patterns

### Singleton Pattern
Just like [`TokenCostTracker`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/token_tracker.py) and [`DurationTracker`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/duration_tracker.py), `CodeMetricsTracker` uses a process-wide singleton pattern:
- `CodeMetricsTracker.get_instance(git_slug=None, git_repo=None)` returns the active singleton.
- If called with `git_slug` or `git_repo` when the instance was already created, it backfills the identifiers without overwriting existing state.
- `CodeMetricsTracker.reset_instance()` completely resets the singleton between test cases or independent pipeline executions.

### Memory & Resource Protection (Oversized File Handling)
Processing multi-gigabyte repositories, minified bundles, or binary blobs through line counters can cause container pods to run out of memory (OOM) or hang.
- **Default file size cap**: Files strictly greater than `200 KB` (`204,800 bytes`) are skipped from line-by-line parsing.
- Skipped files are recorded in `tracker.skipped_large_files`.
- The skipped file count and paths are displayed as a note in both the ASCII summary table and the Markdown migration report.

### Directory Pruning & Exclusion Rules
When scanning the file system, `measure_repository()` automatically prunes directories that do not contain user-authored source code:
- **Version Control & IDEs**: `.git`, `.github`, `.gitlab`, `.svn`, `.hg`, `.idea`, `.vscode`
- **Package & Dependency Caches**: `node_modules`, `bower_components`, `vendor`, `packages`, `Pods`
- **Virtual Environments**: `.venv`, `venv`, `env`, `.env`, `virtualenv`, `.tox`
- **Build Outputs & Binaries**: `target`, `build`, `dist`, `out`, `bin`, `obj`, `__pycache__`, `.pytest_cache`, `.mypy_cache`
- **Internal AUGUR Artifacts**: `tiktoken_cache`, `graph_rag_app`, `compiled_pipelines`

---

## 3. Language & Syntax Engine

### SLOC vs. Comment vs. Blank Classification
The line counter classifies each line into one of three categories:
1. **Blank Line (`blank`)**: Empty lines or lines consisting solely of whitespace characters.
2. **Comment Line (`comment`)**:
   - Single-line comments (e.g. `#` in Python/Shell/YAML, `//` in Java/C++/Go/TS, `--` in SQL).
   - Multiline block comments (e.g. `/* ... */` in C-family languages, `<!-- ... -->` in HTML/XML, `""" ... """` or `''' ... '''` docstrings in Python).
   - Tracks block comment state line-by-line to avoid counting inside-comment lines as code.
3. **Source Lines of Code (`code` / SLOC)**:
   - Any executable statement, import, declaration, or tag that is not purely a comment or whitespace.

### Supported Languages & Extension Matrix

| Language | Primary Extensions | Config Extensions | Single-Line Prefix | Multiline Delimiters |
| :--- | :--- | :--- | :--- | :--- |
| **Python** | `.py`, `.pyw`, `.pyx` | `requirements.txt`, `Pipfile`, `pyproject.toml`, `setup.py`, `setup.cfg` | `#` | `"""..."""`, `'''...'''` |
| **Java** | `.java` | `pom.xml`, `build.gradle`, `build.gradle.kts`, `settings.gradle` | `//` | `/*...*/` |
| **JavaScript / TypeScript** | `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs` | `package.json`, `tsconfig.json`, `webpack.config.js`, `vite.config.js` | `//` | `/*...*/` |
| **Go** | `.go` | `go.mod`, `go.sum`, `go.work` | `//` | `/*...*/` |
| **C / C++** | `.c`, `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp`, `.hxx` | `CMakeLists.txt`, `Makefile`, `configure.ac` | `//` | `/*...*/` |
| **C# / .NET** | `.cs` | `.csproj`, `.sln`, `NuGet.config` | `//` | `/*...*/` |
| **Rust** | `.rs` | `Cargo.toml`, `Cargo.lock` | `//` | `/*...*/` |
| **Ruby** | `.rb`, `.rake` | `Gemfile`, `Gemfile.lock`, `.rubocop.yml` | `#` | `=begin...=end` |
| **PHP** | `.php`, `.phtml` | `composer.json`, `composer.lock` | `//`, `#` | `/*...*/` |
| **Kotlin** | `.kt`, `.kts` | `build.gradle.kts` | `//` | `/*...*/` |
| **Scala** | `.scala`, `.sc` | `build.sbt` | `//` | `/*...*/` |
| **SQL** | `.sql` | `flyway.conf`, `.sqlfluff` | `--` | `/*...*/` |
| **Shell / Bash** | `.sh`, `.bash`, `.zsh` | `.env.example`, `.bashrc` | `#` | None |
| **YAML / Config** | `.yaml`, `.yml` | `.yamllint` | `#` | None |
| **JSON** | `.json` | `.eslintrc.json`, `tsconfig.json` | None | None |
| **HTML / XML** | `.html`, `.htm`, `.xml`, `.xhtml` | `web.xml`, `pom.xml` | None | `<!--...-->` |

---

## 4. Class API Reference

### Lifecycle & Initialization

#### `CodeMetricsTracker.get_instance(git_slug=None, git_repo=None) -> CodeMetricsTracker`
Retrieves or instantiates the global singleton. If an instance already exists, backfills `git_slug` or `git_repo` if not previously set.

#### `CodeMetricsTracker.reset_instance(git_slug=None, git_repo=None) -> CodeMetricsTracker`
Re-initializes the global singleton with fresh counters and sets the given repository metadata.

#### `tracker.reset()`
Resets all internal dictionaries, file counts, line counters, and visited file caches without re-instantiating the object.

---

### Measurement & Parsing Methods

#### `CodeMetricsTracker.count_file_lines(filepath: str, language: Optional[str] = None, ext: Optional[str] = None) -> Dict[str, int]`
Statically counts line categories in a specific file. Returns:
```python
{
    "total": 120,    # Total physical lines in file
    "code": 85,      # Source lines of code (SLOC)
    "comment": 20,   # Comment lines
    "blank": 15      # Whitespace / empty lines
}
```
*Safe Fallback*: If the file cannot be opened, is binary, or encounters an OS error, returns zeros without raising an exception.

#### `tracker.measure_repository(repo_path: str, max_file_size: int = 204800, languages: Optional[List[str]] = None) -> CodeMetricsTracker`
Walks `repo_path`, excludes ignore directories, filters out oversized files, and aggregates file counts and lines of code grouped by language.
- `repo_path`: Path to root directory of cloned repository.
- `max_file_size`: Maximum allowed file size in bytes (default: `200 KB`).
- `languages`: Optional list of language names to restrict measurement to.

#### `tracker.measure_dataframe(df: pd.DataFrame, language: str, config: bool = False) -> CodeMetricsTracker`
Extracts file metrics directly from an in-memory pandas DataFrame containing `file_path` and `code` columns.

---

### Serialization & Cross-Stage Persistence

#### `tracker.to_dict() -> Dict[str, Any]`
Serializes tracker state into a JSON-compatible dictionary.

#### `CodeMetricsTracker.from_dict(data: Dict[str, Any]) -> CodeMetricsTracker`
Reconstructs a new `CodeMetricsTracker` instance from serialized dictionary data.

#### `tracker.save_to_file(filepath: str)`
Atomically writes the serialized state into a UTF-8 JSON file (creating parent directories if needed).

#### `CodeMetricsTracker.load_from_file(filepath: str) -> Optional[CodeMetricsTracker]`
Reads and parses a `project_metrics.json` file from disk.

#### `tracker.load_and_merge(filepath: str, current_stage: Optional[str] = None) -> CodeMetricsTracker`
Loads an external `project_metrics.json` file and merges its counts into the current instance:
- Takes the maximum/union of total files and lines.
- Merges per-language file and line metrics without double-counting identical files.
- Deduplicates `skipped_large_files`.

---

### MLflow Integration

#### `tracker.log_to_mlflow(step: Optional[int] = None)`
Logs key scalar metrics to the active MLflow experiment:
- `project_total_files`: Total file count.
- `project_total_lines`: Total physical line count.
- `project_<language>_files`: Per-language file count.
- `project_<language>_code_lines`: Per-language SLOC count.

#### `tracker.upload_to_mlflow(git_slug: Optional[str] = None, stage: Optional[str] = None, multi_repo: bool = False)`
Uploads `project_metrics.json` to the MLflow artifact repository using `DefaultAssetLoader`.
- Single-repo path: `telemetry/project_metrics.json` or `results/pipelines/<slug>/project_metrics.json`
- Multi-repo path: `results/pipelines/multi-repo/project_metrics.json`

#### `tracker.download_from_mlflow(git_slug: Optional[str] = None, multi_repo: bool = False, current_stage: Optional[str] = None)`
Downloads previously logged `project_metrics.json` from MLflow and merges it into the local singleton.

---

### Formatting & Visual Presentation

#### `tracker.format_summary() -> str`
Generates a formatted ASCII table suitable for container pod console logs.

#### `tracker.format_markdown_section() -> str`
Generates a GitHub-flavored Markdown section under the header:
```markdown
### Project Codebase & Scope Summary
```
Formatted with clean tabular columns: Language, Source Files, Config Files, Total Files, Code Lines (SLOC), Comment Lines, Blank Lines, and Total Lines.

---

### Extraction Helpers

#### `extract_data_generation_code_metrics(candidate_dirs: List[str], tracker: Optional[CodeMetricsTracker] = None) -> CodeMetricsTracker`
Searches a list of candidate directories (e.g. `tmp_source`, `tmp_target`, parent volumes) for `project_metrics.json` and merges found files into the tracker.

---

## 5. End-to-End Pipeline Integration Walkthrough

### Stage 1: Data Generation
Files:
- [`pipelines/base/data_generation.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/base/data_generation.py)
- [`pipelines/kubeflow/data_generation.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py)

1. After `detect_languages(source_path)` executes, `DataGenerationPipeline.run` calls:
   ```python
   code_tr = CodeMetricsTracker.get_instance(git_slug=git_slug, git_repo=git_repo)
   with dur_tracker.measure(stage="Data Generation", step="Measure Code Metrics"):
       code_tr.measure_repository(source_path, languages=detected_languages)
   ```
2. In the `finally:` block:
   ```python
   code_tr.save_to_file(os.path.join(target_path, "project_metrics.json"))
   code_tr.log_to_mlflow()
   code_tr.upload_to_mlflow(git_slug=git_slug, stage="Data Generation", multi_repo=multi_repo)
   logging.info("\n" + code_tr.format_summary())
   ```
3. In Kubeflow `prepare_environment_op`, `project_metrics.json` is measured directly on checkout and saved to `tmp_source`, then picked up by `generate_code_and_meta_op`.

---

### Stage 2: GraphRAG Indexing
Files:
- [`pipelines/base/indexing.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/base/indexing.py)
- [`pipelines/kubeflow/indexing.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/kubeflow/indexing.py)

1. At the beginning of `generate_graphrag_index()`, candidate directories and MLflow are checked for `project_metrics.json` using `find_all_telemetry_files` and `code_tracker.download_from_mlflow()`.
2. Telemetry is immediately saved into `graphrag_source_path` and `graphrag_source_path/output`.
3. In the `finally:` block:
   ```python
   code_tracker.log_to_mlflow()
   code_tracker.upload_to_mlflow(git_slug=git_slug, stage="Indexing", multi_repo=multi_repo)
   logging.info("\n" + code_tracker.format_summary())
   ```

---

### Stage 3: Analysis & Report Generation
Files:
- [`utils/graphrag_utils.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/utils/graphrag_utils.py)
- [`pipelines/base/analysis.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/base/analysis.py)
- [`pipelines/kubeflow/analysis.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/pipelines/kubeflow/analysis.py)

1. In `DependencyAnalyzer.generate_migration_report()`:
   - Upstream metrics are loaded and merged.
   - `code_tracker.format_markdown_section()` produces the Markdown table.
   - Prepend `### Project Codebase & Scope Summary` directly above `### LLM Token Usage & Cost Summary` and `### Pipeline Execution Duration Summary`.
   - Injected before `# Code Migration Plan (JSON)`.
2. In `pipelines/base/analysis.py`, a fallback check verifies `### Project Codebase & Scope Summary` is present in the report and calls `_inject_section` if missing.
3. Final metrics are logged to MLflow and printed to pod stdout.

---

## 6. Artifact Formats & Presentation Outputs

### `project_metrics.json` Schema
```json
{
  "git_slug": "my-org_my-service_main",
  "git_repo": "https://github.com/my-org/my-service",
  "run_id": "run-2026-10-02-abc123",
  "total_repo_files": 48,
  "total_repo_lines": 8420,
  "languages": {
    "java": {
      "source_files": 32,
      "config_files": 4,
      "total_files": 36,
      "code_lines": 5820,
      "comment_lines": 840,
      "blank_lines": 760,
      "total_lines": 7420
    },
    "python": {
      "source_files": 10,
      "config_files": 2,
      "total_files": 12,
      "code_lines": 720,
      "comment_lines": 140,
      "blank_lines": 140,
      "total_lines": 1000
    }
  },
  "skipped_large_files": []
}
```

---

### Console ASCII Summary Table
```
========================================================================================================
                               PROJECT CODEBASE & REPOSITORY METRICS
========================================================================================================
Repository / Slug    : my-org_my-service_main
Total Analyzed Files : 48
Total Physical Lines : 8,420
--------------------------------------------------------------------------------------------------------
Language             Source Files   Config Files   Total Files    Code (SLOC)    Comments       Blanks         Total Lines   
--------------------------------------------------------------------------------------------------------
java                 32             4              36             5,820          840            760            7,420         
python               10             2              12             720            140            140            1,000         
--------------------------------------------------------------------------------------------------------
TOTAL                42             6              48             6,540          980            900            8,420         
========================================================================================================
```

---

### Markdown Report Section (`migration_report.md`)
```markdown
### Project Codebase & Scope Summary
**48** file(s) analyzed across **2** language(s) (**8,420** total lines).

| Language | Source Files | Config Files | Total Files | Code (SLOC) | Comments | Blanks | Total Lines |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **java** | 32 | 4 | 36 | 5,820 | 840 | 760 | 7,420 |
| **python** | 10 | 2 | 12 | 720 | 140 | 140 | 1,000 |
| **TOTAL** | **42** | **6** | **48** | **6,540** | **980** | **900** | **8,420** |
```

---

### MLflow Scalar Metrics
When `tracker.log_to_mlflow()` executes, the following metrics appear in the MLflow Run metrics dashboard:
- `project_total_files`: `48.0`
- `project_total_lines`: `8420.0`
- `project_java_files`: `36.0`
- `project_java_code_lines`: `5820.0`
- `project_python_files`: `12.0`
- `project_python_code_lines`: `720.0`

---

## 7. Unit Testing & Verification

The test suite is located in [`workflows/examples/code_understanding/tests/test_code_metrics_tracker.py`](file:///c:/Dev/augur/workflows/examples/code_understanding/tests/test_code_metrics_tracker.py).

### Running Unit Tests
To run the dedicated unit tests:
```powershell
python -m unittest workflows/examples/code_understanding/tests/test_code_metrics_tracker.py
```

To run the entire repository test suite:
```powershell
python -m unittest discover -s workflows/examples/code_understanding/tests
```

### Test Coverage Highlights
- **Singleton Lifecycle**: Verifies `get_instance()` identity and `reset_instance()` state flushing.
- **Line Parsing Accuracy**:
  - Python `#` comments, blank lines, docstring blocks.
  - Java `//` comments and `/* ... */` multiline license headers and blocks.
  - SQL `--` comments and blank lines.
- **Directory Scanning**:
  - Multi-language directory traversal.
  - Ignore-directory pruning (`.git`, `node_modules`, etc.).
  - Oversized file detection and skipping threshold.
- **DataFrame Ingestion**: Direct measurement of code from pandas DataFrames.
- **Persistence & Serialization**:
  - Roundtrip `to_dict` / `from_dict`.
  - JSON `save_to_file` and `load_from_file`.
  - Upstream / downstream `load_and_merge`.
- **Telemetry Sync**:
  - MLflow metrics logging with stub verification.
  - Asset upload and download mock verification.
- **Formatting**:
  - ASCII terminal table structure.
  - Markdown table generation and warning annotations.
