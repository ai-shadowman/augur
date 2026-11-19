# Single Repo With Context Scan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third run mode that runs the multi-repo scan, stores a repo catalog, then deep-scans one focal repository with its cross-repository imports resolved against that catalog.

**Architecture:** One new pure module, `utils/repo_catalog.py`, builds the catalog from data-generation output and writes an annotated copy of the focal repository. A new pipeline in `pipelines/orchestrator.py` runs the existing multi-repo stages unchanged, then the catalog and annotation step, then the existing single-repo indexing and analysis stages on the annotated copy. Data generation is not touched.

**Tech Stack:** Python 3.11, PyYAML, Kubeflow Pipelines (`kfp`), GraphRAG, MLflow asset loader, `unittest`-style tests run with pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-single-repo-with-context-design.md`

All code paths below are relative to `workflows/examples/code_understanding/`. Run every command from that directory.

## Global Constraints

- **No commits.** The project owner commits. Every task ends by leaving its changes uncommitted and reporting what changed. Do not run `git commit`, `git push`, or `git stash`.
- Work on the branch that is checked out. Do not create or switch branches.
- Do not modify `pipelines/base/data_generation.py` or `pipelines/kubeflow/data_generation.py`. `git diff --stat` must not list them at the end.
- Output of the existing single-repo and multi-repo modes must not change. Every new parameter defaults to a value that reproduces today's behavior exactly.
- No new LLM calls for the catalog or the annotation, and no LLM data generation re-run for the focal repository.
- No new dependencies.
- Names fixed by the spec: flag `--single-repo-with-context`; compile-map key and KFP display name `single_repo_with_context`; `@dsl.pipeline` name `single-repo-with-context-pipeline`; catalog file `repo_catalog.json`; tag `scan_mode` with value `single-repo-with-context`; catalog tag `repo-catalog: true`.
- Only imports are resolved. Declared build dependencies (`pom.xml` and the like) are out of scope.
- Test command (the system Python has no pytest):

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --python 3.11 --with pytest --with pandas --with pyarrow --with pyyaml --with jinja2 --with pip-requirements-parser --with packaging --with mlflow-skinny --with pygments --with requests --with 'pathspec>=1.1.1,<2' python -m pytest -p no:cacheprovider <test paths> -q
```

Below, `$PYTEST <paths>` means that command with those paths. On the full suite, two failures already exist on main and are not regressions: `test_augurignore_data_generation::...test_one_policy_is_reused_across_language_and_config_passes` and `test_duration_tracker::...test_graphrag_evaluation_op_duration_tracking_and_persistence`.

## Review Focus

1. **A sibling package name that is a string prefix but not a path prefix** (`train` vs import `trainer.core`): must not resolve. Pinned in Task 2.
2. **Empty or placeholder metadata values** (`package: ''`, `imports_0: ''`, `runtime_stack_0_name: ''`): must be treated as absent, never matched, never written to the catalog. Pinned in Tasks 1 and 2.
3. **Code below the header that looks like a header line** (a source line reading `- [import] x`): must not be rewritten. Pinned in Task 3.
4. **`repo_catalog.json` left in `<parent_target>` from an earlier run**: must not enter the overview index input. Pinned in Task 4.
5. **Focal repository given with a different URL spelling than the list** (trailing `.git`, trailing slash): the comparison is by slug, so whatever `generate_slug_from_repo` treats as equal must be accepted and anything else rejected with an error naming the focal slug and the slugs in the list. Pinned in Task 1.

## File Structure

| File | Responsibility |
|---|---|
| Create `utils/repo_catalog.py` | Catalog rollup, import resolution, focal annotation, focal validation, catalog storage. No pipeline or KFP imports at module top. |
| Create `tests/test_repo_catalog.py` | Unit tests for all of the above. |
| Create `pipelines/kubeflow/single_repo_with_context.py` | Two KFP components: validate the focal repository, and build catalog plus annotated focal copy. |
| Modify `pipelines/base/indexing.py` | Exclude `repo_catalog` from index input; optional `scan_mode` tag. |
| Modify `pipelines/base/analysis.py` | Optional `scan_mode` tag. |
| Modify `pipelines/kubeflow/indexing.py`, `pipelines/kubeflow/analysis.py` | Pass `scan_mode` through. |
| Modify `pipelines/orchestrator.py` | `single_repo_with_context_pipeline` and its compile-map entry. |
| Modify `assets/graphrag/extract_graph.txt` | One rule for annotated imports. |
| Modify `scripts/run_pipelines.sh`, `README.md` | The new flag and its documentation. |

---

### Task 1: Repo catalog rollup and focal validation

**Files:**
- Create: `utils/repo_catalog.py`
- Test: `tests/test_repo_catalog.py`

**Interfaces:**
- Consumes: `utils.code_utils.generate_slug_from_repo(git_repo, git_branch) -> str`; `provenance.facts.read(path: Path) -> RepoFacts` (fields used: `runtime: str | None`, `packages` with `.name` and `.version`); `provenance.pipeline.FACTS_FILENAME` (`"provenance.json"`). Import these inside the functions, not at module top.
- Produces:
  - `CATALOG_FILENAME = "repo_catalog.json"`, `SCAN_MODE = "single-repo-with-context"`
  - `require_focal_in_list(git_repo: str, git_branch: str, git_repos: list[dict]) -> str` returns the focal slug; raises `ValueError` if `git_repo` is empty or the slug is not among the list's slugs. List items are `{"git_repo": ..., "git_branch": ...}`; a missing `git_branch` means `"main"`.
  - `read_flat_metadata(path: str) -> dict | None` parses one `_metadata.txt` and regroups flattened keys: returns `{"file_path", "language", "package", "purpose", "imports": [...], "classes": [...]}` with empty strings and `None` dropped. Returns `None` when the file is unreadable, is not a YAML mapping, or has no `file_path`.
  - `build_repo_catalog(parent_target_path: str, git_repos: list[dict]) -> dict`

Catalog shape:

```json
{"repos": [{
  "git_repo": "https://github.com/org/train", "git_branch": "main", "git_slug": "org-train-main",
  "languages": ["java"],
  "runtime_stack": ["Java 17"],
  "libraries": [{"name": "junit", "version": "5.10.0"}],
  "packages": [{"package": "com.org.train",
                "files": [{"file_path": "src/Train.java", "purpose": "Models a train.", "classes": ["Train"]}]}]
}]}
```

Decisions the tests do not make obvious:

- `_metadata.txt` is flat YAML written by `utils.json_utils.flatten_code_metadata`: list items are keys `imports_0`, `imports_1`, `classes_0`. Regroup by the regex `^(imports|classes)_\d+$`, ordered by index.
- A repository's metadata is every file ending `_metadata.txt` under `<parent_target_path>/<git_slug>/`, walked recursively, in sorted path order.
- `runtime_stack` is a list of plain strings. Source order: `RepoFacts.runtime` from `<slug>/provenance.json` if set; otherwise the text after `, runtime ` on the header line (the line containing `This file is located at`, looked for in the first three lines) of any generated `.txt` file in the repository, split on ` / `. Deduplicate, keep first-seen order. (The spec says "name and version entries"; one `"Name version"` string per entry is how both sources already express it.)
- `libraries` comes only from `RepoFacts.packages`, deduplicated on `(name, version)`, sorted by name.
- Keys with no data are omitted from a repository entry, except `git_repo`, `git_branch`, `git_slug`, which are always present. Files with no `package` are left out of `packages`.
- A repository directory that does not exist raises `RuntimeError` naming the repository. A missing or unreadable `provenance.json`, or an unreadable `_metadata.txt`, logs a warning and is skipped.

- [ ] **Step 1: Write the failing tests** in `tests/test_repo_catalog.py` (`unittest.TestCase`, `tempfile.TemporaryDirectory`, a helper `write_meta(repo_dir, rel, **fields)` that writes flat YAML with `yaml.safe_dump`):

```python
class TestRequireFocalInList(unittest.TestCase):
    LIST = [{"git_repo": "https://github.com/org/train", "git_branch": "main"},
            {"git_repo": "https://github.com/org/timetable"}]

    def test_returns_slug_of_listed_repo(self):
        self.assertEqual(require_focal_in_list("https://github.com/org/train", "main", self.LIST),
                         generate_slug_from_repo("https://github.com/org/train", "main"))

    def test_list_item_without_branch_means_main(self):
        require_focal_in_list("https://github.com/org/timetable", "main", self.LIST)

    def test_empty_git_repo_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "GIT_REPO"):
            require_focal_in_list("", "main", self.LIST)

    def test_unlisted_repo_is_rejected_naming_both_sides(self):
        with self.assertRaises(ValueError) as ctx:
            require_focal_in_list("https://github.com/org/other", "main", self.LIST)
        self.assertIn(generate_slug_from_repo("https://github.com/org/other", "main"), str(ctx.exception))
        self.assertIn(generate_slug_from_repo("https://github.com/org/train", "main"), str(ctx.exception))

    def test_spelling_variants_follow_the_slug(self):
        # Review Focus 5: accepted exactly when generate_slug_from_repo agrees.
        variant = "https://github.com/org/train.git"
        same = (generate_slug_from_repo(variant, "main")
                == generate_slug_from_repo("https://github.com/org/train", "main"))
        if same:
            require_focal_in_list(variant, "main", self.LIST)
        else:
            with self.assertRaises(ValueError):
                require_focal_in_list(variant, "main", self.LIST)


class TestReadFlatMetadata(unittest.TestCase):
    def test_regroups_lists_and_drops_empty_values(self):
        # file with file_path, language, package, purpose, imports_0='a.b', imports_1='', classes_0='C',
        # runtime_stack_0_name=''
        self.assertEqual(read_flat_metadata(path), {
            "file_path": "src/A.java", "language": "java", "package": "com.a",
            "purpose": "Does A.", "imports": ["a.b"], "classes": ["C"]})

    def test_file_without_file_path_is_none(self): ...
    def test_non_mapping_yaml_is_none(self): ...


class TestBuildRepoCatalog(unittest.TestCase):
    def test_rolls_up_packages_files_and_languages(self): ...   # asserts the shape above for two repos
    def test_files_without_package_are_left_out_of_packages(self): ...
    def test_empty_package_value_is_never_a_package(self): ...  # Review Focus 2
    def test_runtime_and_libraries_come_from_provenance_json(self): ...
        # write provenance.json with provenance.facts' own writer, runtime="Java 17", one package
    def test_runtime_falls_back_to_header_suffix(self): ...
        # no provenance.json; a .txt whose first line ends ", runtime Java 17 / Maven 3.9"
        # -> ["Java 17", "Maven 3.9"]
    def test_missing_optional_data_omits_the_keys(self): ...     # no runtime_stack / libraries keys
    def test_missing_repo_directory_raises_naming_the_repo(self): ...
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `$PYTEST tests/test_repo_catalog.py`
Expected: collection error, `No module named 'utils.repo_catalog'`.

- [ ] **Step 3: Implement** `CATALOG_FILENAME`, `SCAN_MODE`, `require_focal_in_list`, `read_flat_metadata`, `build_repo_catalog` in `utils/repo_catalog.py`.

- [ ] **Step 4: Run them and confirm they pass**

Run: `$PYTEST tests/test_repo_catalog.py`
Expected: all pass.

- [ ] **Step 5: Leave uncommitted.** Report the two files created.

---

### Task 2: Import resolution

**Files:**
- Modify: `utils/repo_catalog.py`
- Test: `tests/test_repo_catalog.py`

**Interfaces:**
- Consumes: the catalog dict from Task 1.
- Produces:
  - `resolve_import(import_name: str, catalog: dict, focal_slug: str) -> list[dict]`. Each match is `{"git_repo", "git_slug", "package", "purpose", "runtime"}`; `purpose` and `runtime` are `""` when unknown. Returns `[]` for no match. Matches are sorted by `git_slug`.
  - `format_annotation(matches: list[dict]) -> str`

Rules (spec, "Import Resolution"):

- A package matches when it equals the import, or the import starts with the package followed by one of `.`, `/`, `::`.
- Only the longest matching package length counts. All siblings matching at that length are returned (a tie gives two matches).
- Packages of the repository whose slug is `focal_slug` never match.
- An import starting with `.` never resolves. An empty import never resolves.
- Matching is case-sensitive.
- `purpose`: if exactly one file in the matched package has a basename, without extension, equal to the import's last segment (split on `.`, `/`, `::`), use that file's purpose. Otherwise join the purposes of the package's first three files in `file_path` order with `"; "`. Either way collapse whitespace to single spaces and truncate to 200 characters.
- `runtime`: the repository's `runtime_stack` joined with `" / "`.

Annotation format, one parenthesised group per match, groups separated by one space. The `purpose` and `runtime` clauses are omitted when empty:

```
(provided by repository https://github.com/org/train, slug org-train-main; purpose: Models a train.; runtime: Java 17)
```

- [ ] **Step 1: Write the failing tests**

```python
class TestResolveImport(unittest.TestCase):
    # CATALOG: focal "org-timetable-main" with package "com.org.timetable";
    # sibling "org-train-main" with packages "com.org.train" (Train.java "Models a train.",
    # Car.java "Models a car.") and "com.org.train.api" (Routes.java "HTTP routes.");
    # sibling "org-rail-main" with package "com.org.train" too (Rail.java "Rail things."),
    # runtime_stack ["Java 17"] on org-train-main only.

    def test_exact_match(self):            # "com.org.train.api" -> one match, package "com.org.train.api"
    def test_prefix_match_with_dot(self):  # "com.org.train.api.Routes" -> package "com.org.train.api",
                                           #   purpose "HTTP routes."
    def test_wildcard_import(self):        # "com.org.train.api.*" -> package "com.org.train.api"
    def test_longest_package_wins(self):   # "com.org.train.api.Routes" never returns "com.org.train"
    def test_tie_returns_both_sorted_by_slug(self):
        # "com.org.train.Train" -> ["org-rail-main", "org-train-main"]
    def test_string_prefix_that_is_not_a_path_prefix_does_not_match(self):
        # Review Focus 1: "com.org.trainer.Core" -> []
    def test_slash_and_double_colon_separators(self):
        # package "lib/train" matches "lib/train/car"; package "train" matches "train::car"
    def test_own_package_never_matches(self):   # "com.org.timetable.Model" -> []
    def test_relative_import_never_resolves(self):  # "..train" -> []
    def test_empty_import_never_resolves(self):     # "" -> []
    def test_no_match(self):                        # "org.junit.Test" -> []
    def test_file_purpose_used_when_last_segment_names_a_file(self):
        # "com.org.train.Car" on org-train-main -> purpose "Models a car."
    def test_package_purpose_joins_first_three_files(self):
        # "com.org.train.*" on org-train-main -> "Models a car.; Models a train."
    def test_purpose_is_one_line_and_at_most_200_chars(self):
    def test_runtime_joined(self):                  # "Java 17"


class TestFormatAnnotation(unittest.TestCase):
    def test_full(self):
        self.assertEqual(
            format_annotation([{"git_repo": "https://github.com/org/train", "git_slug": "org-train-main",
                                "package": "com.org.train", "purpose": "Models a train.", "runtime": "Java 17"}]),
            "(provided by repository https://github.com/org/train, slug org-train-main; "
            "purpose: Models a train.; runtime: Java 17)")
    def test_omits_empty_purpose_and_runtime(self):
        # -> "(provided by repository https://github.com/org/train, slug org-train-main)"
    def test_two_matches_are_two_groups_separated_by_a_space(self):
    def test_no_matches_is_empty_string(self):
```

- [ ] **Step 2: Run and confirm they fail**

Run: `$PYTEST tests/test_repo_catalog.py -k "ResolveImport or FormatAnnotation"`
Expected: `ImportError` for `resolve_import`.

- [ ] **Step 3: Implement** `resolve_import` and `format_annotation`.

- [ ] **Step 4: Run and confirm they pass**

Run: `$PYTEST tests/test_repo_catalog.py`
Expected: all pass.

- [ ] **Step 5: Leave uncommitted.**

---

### Task 3: Annotated focal copy

**Files:**
- Modify: `utils/repo_catalog.py`
- Test: `tests/test_repo_catalog.py`

**Interfaces:**
- Consumes: `read_flat_metadata`, `resolve_import`, `format_annotation`.
- Produces: `annotate_focal_repo(source_dir: str, dest_dir: str, catalog: dict, focal_slug: str) -> dict` returning `{"files": int, "resolved_imports": int, "unresolved_imports": int}`.

Behavior:

- `dest_dir` is removed if it exists, then `source_dir` is copied to it whole. `source_dir` is never written to. `source_dir` missing or containing no `.txt` file raises `RuntimeError`.
- Work is driven by each `<stem>_metadata.txt` in the copy and its code file `<stem>.txt` beside it.
- **Code file.** The header line is the first of the file's first three lines that contains `This file is located at` (the comment delimiter may or may not share its line). On that line only, replace the first `multi_repo true` or `multi_repo false` with `multi_repo false`. If there is no header line, or it has neither, leave the file's flag alone and log a warning. Then find the first line whose stripped text is `Dependencies:`. In the run of lines directly after it that start with `- [`, a line exactly equal to `- [import] <name>`, where `<name>` resolves, becomes `- [import] <name> <annotation>`. Stop at the first line not starting with `- [`. Nothing else in the file changes, including line endings and the final newline.
- **Metadata file.** Load the flat YAML, set `multi_repo` to `False`, and for each `imports_N` whose value resolves, set it to `<name> <annotation>`. Write it back with `yaml.dump(data, default_flow_style=False, allow_unicode=True)` after importing `utils.json_utils`, which registers the multi-line string representer the original was written with.
- A metadata file with no code file beside it still has its metadata rewritten. Files that are neither are copied untouched.
- Counts: `files` is the number of metadata files processed; an import counts once per file, as resolved or unresolved.

- [ ] **Step 1: Write the failing tests**

```python
class TestAnnotateFocalRepo(unittest.TestCase):
    # Fixture: source_dir with src/Model.txt:
    #   '/*This file is located at src/Model.java from repository url U, repository slug S, multi_repo true\n'
    #   '\n Package: com.org.timetable\n\nDependencies:\n'
    #   '- [import] com.org.train.Train\n- [import] java.util.List\n- [library] junit 5\n*/\n'
    #   'import com.org.train.Train;\n// - [import] com.org.train.Train\n'
    # and src/Model_metadata.txt (flat YAML: file_path, package, multi_repo 'true',
    #   imports_0 com.org.train.Train, imports_1 java.util.List), plus provenance.json.

    def test_first_line_flag_is_forced_false(self):
    def test_flag_already_false_is_left_false(self):
    def test_resolved_import_line_gets_the_annotation(self):
        # line == "- [import] com.org.train.Train " + format_annotation(matches)
    def test_unresolved_import_and_library_lines_are_byte_identical(self):
    def test_only_the_flag_and_resolved_lines_differ(self):
        # diff source vs dest line by line: exactly the header line and the one resolved import line differ
    def test_header_line_on_second_line_is_still_found(self):
        # same fixture with the opening delimiter alone on line 1
    def test_header_lookalike_in_code_body_is_not_rewritten(self):   # Review Focus 3
        # the "// - [import] ..." line and a body line "- [import] com.org.train.Train" after the
        # header block are unchanged
    def test_file_without_dependencies_block_only_changes_the_flag(self):
    def test_metadata_file_gets_flag_false_and_annotated_import(self):
        # yaml.safe_load(dest metadata): multi_repo is False, imports_0 == name + " " + annotation,
        # imports_1 == "java.util.List"
    def test_source_dir_is_not_modified(self):      # hash every file before and after
    def test_other_files_are_copied_untouched(self):  # provenance.json bytes equal
    def test_existing_dest_dir_is_replaced(self):     # stale file in dest is gone
    def test_counts(self):   # {"files": 1, "resolved_imports": 1, "unresolved_imports": 1}
    def test_missing_source_dir_raises(self):
    def test_source_dir_without_txt_files_raises(self):
```

- [ ] **Step 2: Run and confirm they fail**

Run: `$PYTEST tests/test_repo_catalog.py -k AnnotateFocalRepo`
Expected: `ImportError` for `annotate_focal_repo`.

- [ ] **Step 3: Implement** `annotate_focal_repo`.

- [ ] **Step 4: Run and confirm they pass**

Run: `$PYTEST tests/test_repo_catalog.py`
Expected: all pass.

- [ ] **Step 5: Leave uncommitted.**

---

### Task 4: Catalog storage and index-input exclusion

**Files:**
- Modify: `utils/repo_catalog.py`
- Modify: `pipelines/base/indexing.py` (the `excluded_files` set in `generate_graphrag_index`, near line 166)
- Test: `tests/test_repo_catalog.py`, `tests/test_data_generation_telemetry.py`

**Interfaces:**
- Consumes: `DefaultAssetLoader().log_results(results_path, artifact_path=..., tags=...)`; `DefaultAssetLoader.get_log_results_artifact_path(DefaultAssetLoader.RESULTS_PATH_PREFIX_METADATA, git_slug=None, multi_repo=True)`.
- Produces: `save_repo_catalog(catalog: dict, parent_target_path: str) -> str` writes `<parent_target_path>/repo_catalog.json` (indent 2, `sort_keys=True`), logs it, and returns the path.

Tags, exactly: `{"category": "data-generation", "multi_repo": "True", "repo-catalog": "true"}`. A logging failure is logged as a warning and does not raise; the file on disk is what the deep pass reads.

- [ ] **Step 1: Write the failing tests**

In `tests/test_repo_catalog.py`, patching `loaders.default_asset_loader.DefaultAssetLoader` the way `tests/test_multi_repo_artifact_downloads.py` stubs loaders:

```python
class TestSaveRepoCatalog(unittest.TestCase):
    def test_writes_json_and_returns_path(self):
        # returned path == os.path.join(tmp, "repo_catalog.json"); json.load(path) == catalog
    def test_logs_under_multi_repo_metadata_path_with_tags(self):
        # log_results called once with tags == {"category": "data-generation",
        #   "multi_repo": "True", "repo-catalog": "true"}
    def test_logging_failure_does_not_raise(self):
```

In `tests/test_data_generation_telemetry.py`, next to the existing test that asserts `Main_metadata.txt` stays in the index input (near line 560), add `test_repo_catalog_is_removed_from_index_input`: put `repo_catalog.json` at the codebase root, run the same path that test runs, and assert the file is absent from `<graphrag>/input` while `Main_metadata.txt` is still present. (Review Focus 4.)

- [ ] **Step 2: Run and confirm they fail**

Run: `$PYTEST tests/test_repo_catalog.py tests/test_data_generation_telemetry.py -k "SaveRepoCatalog or repo_catalog_is_removed"`
Expected: `ImportError` for `save_repo_catalog`; the indexing test fails because the file is still in `input`.

- [ ] **Step 3: Implement** `save_repo_catalog`, and add `"repo_catalog"` to the `base_t` list that builds `excluded_files` in `pipelines/base/indexing.py`.

- [ ] **Step 4: Run and confirm they pass**

Run: `$PYTEST tests/test_repo_catalog.py tests/test_data_generation_telemetry.py`
Expected: all pass.

- [ ] **Step 5: Leave uncommitted.**

---

### Task 5: `scan_mode` tag through indexing and analysis

**Files:**
- Modify: `pipelines/base/indexing.py`: `generate_graphrag_index`, `IndexingPipeline.run`
- Modify: `pipelines/base/analysis.py`: `AnalysisPipeline.run`, `write_migration_report`
- Modify: `pipelines/kubeflow/indexing.py`: `graphrag_indexing_op`, `_run_pipeline`
- Modify: `pipelines/kubeflow/analysis.py`: `generate_migration_report_op`, `_run_pipeline`
- Test: `tests/test_data_generation_telemetry.py`, `tests/test_token_tracker.py`

**Interfaces:**
- Produces: every function above gains a trailing keyword parameter `scan_mode: str = ""` and passes it down. When it is non-empty, `"scan_mode": scan_mode` is added to the `tags` dict of each `log_results` call in `generate_graphrag_index` (two calls, near lines 278 and 311) and in `AnalysisPipeline.run` (near line 164). When it is empty, the `tags` dicts are exactly what they are today: no `scan_mode` key.
- Do not thread it into `evaluate_graphrag_index`, `graphrag_evaluation_op`, the trackers, or any multi-repo function.

- [ ] **Step 1: Write the failing tests.** Follow the existing tests that patch `DefaultAssetLoader` and inspect `log_results` calls (`tests/test_data_generation_telemetry.py` for indexing, `tests/test_token_tracker.py::test_analysis_pipeline_run_multi_repo_returns_report` for analysis):

```python
def test_indexing_tags_carry_scan_mode_when_given(self):
    # generate_graphrag_index(..., scan_mode="single-repo-with-context")
    # every log_results call's tags["scan_mode"] == "single-repo-with-context"
def test_indexing_tags_have_no_scan_mode_key_by_default(self):
    # for every log_results call: "scan_mode" not in tags
def test_analysis_tags_carry_scan_mode_when_given(self):
def test_analysis_tags_have_no_scan_mode_key_by_default(self):
```

- [ ] **Step 2: Run and confirm the two "when_given" tests fail**

Run: `$PYTEST tests/test_data_generation_telemetry.py tests/test_token_tracker.py -k scan_mode`
Expected: `TypeError: ... unexpected keyword argument 'scan_mode'`.

- [ ] **Step 3: Implement** the parameter and the conditional tag in the base functions, then pass it through the four Kubeflow functions.

- [ ] **Step 4: Run the whole suite**

Run: `$PYTEST tests`
Expected: only the two known failures listed under Global Constraints.

- [ ] **Step 5: Leave uncommitted.**

---

### Task 6: Extraction rule for annotated imports

**Files:**
- Modify: `assets/graphrag/extract_graph.txt`
- Test: `tests/test_repo_catalog.py`

**Interfaces:**
- Consumes: the annotation wording from Task 2 (`provided by repository <url>, slug <slug>`).

Add this rule to step 1 of `-Steps-`, directly after the three-layer block (after the line `If unsure between b and c, use library. Never leave entity_type blank.`), at the same indentation as the `- Before typing an imported or referenced name` bullet:

```
- An import line in the header may end with one or more annotations of the form
  "(provided by repository <url>, slug <slug>; purpose: <text>; runtime: <text>)". Such an
  import is application code of ANOTHER repository, not a library. Type it module, name it
  <slug>::<imported name> using the slug from the annotation whatever multi_repo says, and
  put the purpose and runtime in its description. Relate the importing module to it with
  CROSS_REPO_IMPORTS. Never emit the same import as a library as well. If there are several
  annotations, emit one such entity per annotation. An import line with no annotation is
  unaffected by this rule.
```

- [ ] **Step 1: Write the failing test**

```python
class TestExtractGraphPromptRule(unittest.TestCase):
    PROMPT = (Path(__file__).resolve().parents[1] / "assets/graphrag/extract_graph.txt").read_text()

    def test_prompt_recognises_the_annotation_format_annotation_writes(self):
        annotation = format_annotation([{"git_repo": "U", "git_slug": "S", "package": "p",
                                         "purpose": "", "runtime": ""}])
        self.assertTrue(annotation.startswith("(provided by repository "))
        self.assertIn("(provided by repository <url>, slug <slug>", self.PROMPT)

    def test_rule_names_the_relationship_and_the_no_annotation_guard(self):
        self.assertIn("An import line with no annotation is\n  unaffected by this rule.", self.PROMPT)
        rule = self.PROMPT[self.PROMPT.index("(provided by repository <url>"):]
        self.assertIn("CROSS_REPO_IMPORTS", rule[:700])
```

- [ ] **Step 2: Run and confirm it fails**

Run: `$PYTEST tests/test_repo_catalog.py -k ExtractGraphPromptRule`
Expected: `AssertionError` on the first `assertIn`.

- [ ] **Step 3: Add the rule** to `assets/graphrag/extract_graph.txt` exactly as above.

- [ ] **Step 4: Run and confirm it passes, and that prompt tests elsewhere still pass**

Run: `$PYTEST tests/test_repo_catalog.py tests/test_provenance_graph.py tests/test_provenance_stitch.py`
Expected: all pass.

- [ ] **Step 5: Check provenance grounding by reading, and report.** In `provenance/graph.py` and `provenance/stitch.py`, establish what happens in a `multi_repo=False` index to (a) a `module` entity whose title is `<other-slug>::<name>` and (b) a relationship described as `CROSS_REPO_IMPORTS`. Report one of: kept as is, renamed, merged into the focal repository, or dropped, with the function and line. Do not change provenance code. If it is renamed, merged, or dropped, stop and report to the owner: the spec's fallback (sibling left typed as `library` with an enriched description) changes this task's rule text and is the owner's call.

- [ ] **Step 6: Leave uncommitted.**

---

### Task 7: Local pipeline, Kubeflow pipeline, and run flag

**Files:**
- Create: `pipelines/kubeflow/single_repo_with_context.py`
- Modify: `pipelines/orchestrator.py`
- Modify: `scripts/run_pipelines.sh`
- Modify: `README.md`
- Test: `tests/test_repo_catalog.py`

**Interfaces:**
- Consumes: everything in `utils/repo_catalog.py`; `IndexingPipeline.run(..., scan_mode=)` and `AnalysisPipeline.run(..., scan_mode=)` from Task 5; `utils.loader_utils.download_code_metadata_directories(git_repos, parent_target_path)`; `utils.kubeflow_utils.write_to_output_artifact`.
- Produces:
  - `utils.repo_catalog.prepare_focal_codebase(git_repo: str, git_branch: str, git_repos: list[dict], parent_target_path: str, dest_dir: str) -> dict`: calls `require_focal_in_list`, `build_repo_catalog`, `save_repo_catalog`, then `annotate_focal_repo(f"{parent_target_path}/{focal_slug}", dest_dir, catalog, focal_slug)`; logs the counts at INFO; returns the counts. Both variants call this one function.
  - `pipelines/kubeflow/single_repo_with_context.py`: `validate_focal_repo_op(git_repo: str, git_branch: str)` and `annotate_focal_repo_op(git_repo: str, git_branch: str, parent_target_path: str, codebase_dir: Output[Dataset])`.
  - `pipelines/orchestrator.py`: `single_repo_with_context_pipeline(git_repo, git_branch, parent_source_path, parent_target_path)` with the same parameter defaults as `single_repo_pipeline` (no `multi_repo` parameter), decorated `@dsl.pipeline(name="single-repo-with-context-pipeline")`, and `"single_repo_with_context": single_repo_with_context_pipeline` in the `compile_all_and_exit` map.

**Local branch** of `single_repo_with_context_pipeline` (the `else` of `uses_kfp()`), in this order:

1. Load `git_repos` exactly as `multi_repo_pipeline` does.
2. `focal_slug = require_focal_in_list(git_repo, git_branch, git_repos)`. This is the fail-before-any-work check.
3. The three calls `multi_repo_pipeline` makes: `DataGenerationPipeline().run_multi_repo(git_repos)`, `IndexingPipeline().run_multi_repo(parent_target_path=parent_target_path)`, `AnalysisPipeline().run_multi_repo()`.
4. `annotated_path = f"{parent_target_path}-single-repo-with-context/{focal_slug}"`, a sibling of `parent_target_path` and never inside it, so the overview index cannot pick it up on a later run. `prepare_focal_codebase(git_repo, git_branch, git_repos, parent_target_path, annotated_path)`.
5. `graphrag_source_path = os.path.join(os.getenv("KFP_DATA_INDEXING_OUTPUT_PATH", "graph_rag_app/source"), focal_slug)`, as `single_repo_pipeline` builds it.
6. `IndexingPipeline().run(codebase_path=annotated_path, graphrag_source_path=graphrag_source_path, git_repo=git_repo, git_branch=git_branch, multi_repo=False, scan_mode=SCAN_MODE)`. The base `run` returns a status dict and does not raise: if `result.get("status") != "success"`, raise `RuntimeError` with its `fail_message`.
7. `AnalysisPipeline().run(graphrag_source_path=graphrag_source_path, git_repo=git_repo, git_branch=git_branch, multi_repo=False, scan_mode=SCAN_MODE)`.

**Kubeflow branch:**

```python
check = validate_focal_repo_op(git_repo=git_repo, git_branch=git_branch)
dg = DataGenerationPipeline.run_multi_repo().after(check)
idx = IndexingPipeline.run_multi_repo(parent_target_path=parent_target_path).after(dg)
AnalysisPipeline.run_multi_repo(graphrag_dir=idx.outputs["graphrag_dir"])
focal = annotate_focal_repo_op(git_repo=git_repo, git_branch=git_branch,
                               parent_target_path=parent_target_path).after(dg)
fidx = IndexingPipeline.run(codebase_dir=focal.outputs["codebase_dir"], git_repo=git_repo,
                            git_branch=git_branch, multi_repo=False, scan_mode=SCAN_MODE)
AnalysisPipeline.run(graphrag_dir=fidx.output, git_repo=git_repo, git_branch=git_branch,
                     multi_repo=False, scan_mode=SCAN_MODE)
```

The two components:

- Decorate both like `run_indexing_multi_repo_op` in `pipelines/kubeflow/indexing.py` (`@dsl.component(base_image=INDEXING_BASE_IMAGE, install_kfp_package=False)`), with all imports inside the function body and `setup_logging()` first, as every op in that file does.
- Both read the list with `json.loads(os.getenv("GIT_REPO_LIST_CONTENTS"))`, as `get_repo_list_op` does. No asset-loader fallback under Kubeflow.
- `validate_focal_repo_op` calls `require_focal_in_list` and nothing else.
- `annotate_focal_repo_op` calls `download_code_metadata_directories(git_repos, parent_target_path)`, then `prepare_focal_codebase(...)` with `dest_dir` set to the directory yielded by `write_to_output_artifact(codebase_dir)`. Note `annotate_focal_repo` deletes and recreates `dest_dir`; if `write_to_output_artifact` needs the directory it yields to keep existing, annotate into a subdirectory of a temp dir and copy the result in instead.
- In the KFP compile-time `uses_kfp()` import block at the top of `orchestrator.py`, import the two ops only in the `if uses_kfp():` arm.

**`scripts/run_pipelines.sh`:**

- Add `--single-repo-with-context) MODE="single_repo_with_context" ;;` to the `case`.
- Add a branch: when `MODE` is `single_repo_with_context`, require non-empty `GIT_REPO` with the message `Error: GIT_REPO must be set and non-empty for --single-repo-with-context.`, then `trigger_pipeline "single_repo_with_context" "single_repo_with_context_${TIMESTAMP}"` with the same four-key JSON params as the single-repo branch.
- Add the flag to the header comment and to `usage`, described as: `Trigger single-repo-with-context pipeline run: multi-repo scan, then deep scan of GIT_REPO (GIT_REPO required and must be in the repo list)`.
- The default mode stays `multi`.

**`README.md`:** in the section that describes the single- and multi-repository runs, add a short paragraph for the new mode: what it does, that `GIT_REPO` must be one of the listed repositories, that only imports are resolved (not declared build dependencies), that results are stored on the single-repo paths for the focal repository and replace or are replaced by a plain single-repo run of it, and that `repo_catalog.json` is logged with the multi-repo metadata.

- [ ] **Step 1: Write the failing tests**

```python
class TestPrepareFocalCodebase(unittest.TestCase):
    # two-repo fixture on disk under parent_target, DefaultAssetLoader patched
    def test_writes_catalog_and_annotated_copy_and_returns_counts(self):
        # repo_catalog.json exists in parent_target; dest_dir has the annotated focal file;
        # result == {"files": 1, "resolved_imports": 1, "unresolved_imports": 1}
    def test_unlisted_focal_fails_before_anything_is_written(self):
        # ValueError; no repo_catalog.json; dest_dir does not exist


class TestOrchestratorWiring(unittest.TestCase):
    SRC = (Path(__file__).resolve().parents[1] / "pipelines/orchestrator.py").read_text()

    def test_pipeline_is_defined_and_registered(self):
        tree = ast.parse(self.SRC)
        self.assertIn("single_repo_with_context_pipeline",
                      [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)])
        self.assertIn('"single_repo_with_context"', self.SRC)
        self.assertIn('name="single-repo-with-context-pipeline"', self.SRC)

    def test_existing_pipelines_are_still_registered(self):
        for key in ('"single_repo"', '"multi_repo"', '"data_generation"', '"indexing"', '"analysis"'):
            self.assertIn(key, self.SRC)


class TestRunScriptFlag(unittest.TestCase):
    SH = (Path(__file__).resolve().parents[1] / "scripts/run_pipelines.sh").read_text()
    def test_flag_and_pipeline_name(self):
        self.assertIn('--single-repo-with-context) MODE="single_repo_with_context"', self.SH)
        self.assertIn('trigger_pipeline "single_repo_with_context"', self.SH)
        self.assertIn('MODE="multi"\n', self.SH)   # default unchanged
```

- [ ] **Step 2: Run and confirm they fail**

Run: `$PYTEST tests/test_repo_catalog.py -k "PrepareFocalCodebase or OrchestratorWiring or RunScriptFlag"`
Expected: `ImportError` for `prepare_focal_codebase`; assertion failures for the other two classes.

- [ ] **Step 3: Implement** `prepare_focal_codebase`, the two components, the pipeline, the script changes, and the README paragraph.

- [ ] **Step 4: Run the whole suite**

Run: `$PYTEST tests`
Expected: only the two known failures.

- [ ] **Step 5: Shell check**

Run: `bash -n scripts/run_pipelines.sh && GIT_REPO= bash scripts/run_pipelines.sh --single-repo-with-context; echo "exit=$?"`
Expected: no syntax error; the `GIT_REPO must be set and non-empty for --single-repo-with-context` message; `exit=1`. If the script exits earlier on an unrelated missing variable (for example `KFP_HOST`), report the message it gave instead of working around it.

- [ ] **Step 6: Compile check**

Run, with a scratch output directory outside the repository:

```sh
PIPELINE_COMPILE_ONLY=1 KFP_PIPELINE_OUTPUT_DIR=<scratch dir> PYTHONPATH=. python3 pipelines/orchestrator.py
```

Expected: `<scratch dir>` contains `single_repo_with_context.yaml` beside the five existing pipelines, and in it the tasks for `validate-focal-repo-op` and `annotate-focal-repo-op` carry the same injected environment and `imagePullPolicy` as `run-indexing-multi-repo-op` in `multi_repo.yaml`. If `kfp` and the project's dependencies are not installed locally, do not install them into the repository's environment: report this step as not run.

- [ ] **Step 7: Confirm the untouched files**

Run: `git diff --stat -- pipelines/base/data_generation.py pipelines/kubeflow/data_generation.py`
Expected: no output.

- [ ] **Step 8: Leave uncommitted.** Report every file created or modified across the plan.

---

### Task 8: Sample run (owner-run; needs the cluster and an LLM endpoint)

No code. This is the acceptance check from the spec and the test of its main risk: whether LLM-extracted `package` and `imports` values line up across repositories.

**Inputs:** the repository list in `assets/repos/repo_list1.json`; `GIT_REPO=https://github.com/kfrankli/northpolesouthern-timetable`, `GIT_BRANCH=main`. `timetable` is the focal repository because it imports `com.northpolesouthern.train.*`.

- [ ] **Step 1:** Run `./scripts/run_pipelines.sh --single-repo-with-context` (or the local orchestrator path) with those inputs.
- [ ] **Step 2:** From the annotate step's log, record `files`, `resolved_imports`, `unresolved_imports`. Open `repo_catalog.json` and check that the `train` repository has a `packages` entry starting `com.northpolesouthern.train`.
- [ ] **Step 3:** If `resolved_imports` is 0, the package names did not line up. Record what `package` values the catalog holds for `train` and what the `imports` values look like in a `timetable` `_metadata.txt`, and stop: this goes back to the owner. `RepoFacts.own_modules` in each repository's `provenance.json` is a deterministic alternative source of package names worth comparing at that point.
- [ ] **Step 4:** In the focal migration report, record whether `train` is described as a sibling repository with its purpose, or as an unknown or external library.
- [ ] **Step 5:** Record whether `train` also appears a second time as an ordinary library (from `com.northpolesouthern:train 2.0` in `pom.xml`), and whether that muddies the report. The spec makes this the deciding input for a follow-up on build-dependency matching.
- [ ] **Step 6:** Run a plain `--multi-repo` and a plain `--single-repo` on the same inputs and confirm their reports have the same sections and structure as before this change.
