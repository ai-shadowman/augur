# `.augurignore` Repository Exclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional root `.augurignore` whose Git-compatible rules prevent ignored repository content from entering any Augur analysis traversal.

**Architecture:** A per-repository `RepositoryIgnorePolicy` will compile the root file with `pathspec.GitIgnoreSpec`, normalize paths, prune directories, filter files, and collect unique exclusion counts. Base and Kubeflow ingestion will create one policy per repository-processing context and explicitly pass it through language detection, external metadata loading, and source/config dataset generation.

**Tech Stack:** Python 3.11+, `pathspec>=1.1.1,<2`, `unittest`, Git `check-ignore --no-index`, Kubeflow Pipelines 2.17

**Spec:** `docs/superpowers/specs/2026-10-01-augurignore-design.md`

## Global Constraints

- Recognize only `<repository-root>/.augurignore`; nested files have no configuration meaning.
- The client commits the file before Augur runs; do not add runtime pattern inputs or client-side creation logic.
- Decode the file as UTF-8 and use `pathspec.GitIgnoreSpec` for Git-compatible matching.
- Apply custom exclusions in addition to existing built-in exclusions; custom negation cannot override built-ins.
- Load and validate the policy after checkout but before language detection or content reads.
- An absent or empty file adds no exclusions; an unreadable, undecodable, or unparseable file fails that repository.
- Ignored contents must not be opened, analyzed, sent to an LLM, or emitted into generated datasets.
- Ignore rules do not change clone/download behavior, supported languages, supported extensions, or retry orchestration.
- Keep the implementation focused; simplify touched control flow when doing so removes duplication, but do not refactor unrelated pipeline behavior.

## Review Focus

- An excluded parent followed by a negated child must remain excluded, matching Git's traversal constraint; Task 1 pins this with unit and Git-conformance cases.
- Root-anchored patterns must not match the same path at a nested level, and nested `.augurignore` files must not affect policy; Task 1 tests both cases.
- CRLF, non-ASCII paths, missing final newlines, and invalid UTF-8 must produce deterministic matching or the specified configuration error; Task 1 covers each input.
- Paths with platform separators or paths outside the repository root must not bypass or confuse matching; Task 1 tests normalization and rejects out-of-root candidates.
- A configuration error or all-content-excluded error in Kubeflow multi-repo mode must fail the affected task instead of being swallowed by the existing `multi_repo` exception branch; Task 3 verifies this behavior and pipeline compilation.

## File Structure

- Create `workflows/examples/code_understanding/utils/repository_ignore.py`: filename constant, configuration error, immutable stats view, policy loading, matching, filtering, normalization, counters, and summary logging.
- Create `workflows/examples/code_understanding/tests/test_repository_ignore.py`: policy unit tests and Git conformance tests.
- Create `workflows/examples/code_understanding/tests/test_augurignore_data_generation.py`: base-pipeline traversal, content-read, metadata, and no-content integration tests.
- Modify `workflows/examples/code_understanding/utils/code_utils.py`: apply the policy during language-detection traversal.
- Modify `workflows/examples/code_understanding/pipelines/base/data_generation.py`: remove unrestricted clone enumeration, create/share the policy, and apply it to every repository read.
- Modify `workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py`: create/share the policy after artifact extraction and propagate configuration failures.
- Modify `workflows/examples/code_understanding/pyproject.toml`: declare the installable package dependency.
- Modify `resources/images/data-generation/requirements.txt`: install the same dependency in the data-generation image.
- Modify `workflows/examples/code_understanding/README.md`: document the root file, syntax, precedence, failure behavior, and example.

## Execution Topology and Review Protocol

Execution must start from a Herdr-managed primary pane with `HERDR_ENV=1`. Before implementation, invoke `superpowers:using-git-worktrees` and give each development pane an isolated worktree/branch so concurrent changes cannot overwrite one another.

- Development pane `augurignore-policy`: Codex `gpt-5.6-sol`, medium effort. Owns Task 1 and Task 4.
- Development pane `augurignore-pipeline`: Codex `gpt-5.6-sol`, medium effort. Owns Task 2 and Task 3.
- Final review pane `augurignore-review`: Codex `gpt-5.6-sol`, xhigh effort. Performs a read-only whole-branch review after all development commits have passed orchestrator gates.

The primary pane remains the orchestrator. After every task, the owning pane must report its commit and exact verification output. The orchestrator must inspect the diff against this plan and the spec, run that task's verification commands independently, and either request focused corrections/simplification or accept the commit. Only the orchestrator integrates accepted commits.

Task 2 starts after the orchestrator accepts Task 1 and makes that policy commit available in the pipeline worktree. Task 4 may proceed in the policy pane while Tasks 2-3 run because it owns only the README. After all accepted commits are combined, the orchestrator runs the full verification set and hands the resulting branch to `augurignore-review`. Review findings return to the owning development pane; the orchestrator re-verifies every correction. Material corrections receive a final reviewer re-check.

---

### Task 1: Git-Compatible Repository Ignore Policy

**Owner:** `augurignore-policy`

**Files:**
- Create: `workflows/examples/code_understanding/utils/repository_ignore.py`
- Create: `workflows/examples/code_understanding/tests/test_repository_ignore.py`
- Modify: `workflows/examples/code_understanding/pyproject.toml:5-10`
- Modify: `resources/images/data-generation/requirements.txt:1-37`

**Interfaces:**
- Consumes: `pathspec.GitIgnoreSpec.from_lines(lines)` and repository-relative paths.
- Produces: `AUGURIGNORE_FILENAME = ".augurignore"`.
- Produces: `class RepositoryIgnoreError(ValueError)` for load, decode, normalization, and compile failures.
- Produces: immutable `IgnoreStats(ignored_directories: int, ignored_files: int)`.
- Produces: `RepositoryIgnorePolicy.from_repository(repository_root: str | os.PathLike[str]) -> RepositoryIgnorePolicy`.
- Produces: `RepositoryIgnorePolicy.is_ignored(path: str | os.PathLike[str], *, is_directory: bool = False) -> bool`.
- Produces: `RepositoryIgnorePolicy.filter_directories(parent_dir: str | os.PathLike[str], names: list[str], *, built_in_names: Collection[str] = ()) -> list[str]`.
- Produces: `RepositoryIgnorePolicy.filter_files(parent_dir: str | os.PathLike[str], names: Iterable[str], *, built_in_names: Collection[str] = ()) -> list[str]`.
- Produces: read-only `active_pattern_count: int`, `has_patterns: bool`, and `stats: IgnoreStats` properties plus `log_summary() -> None`.

- [ ] **Step 1: Declare the matcher dependency**

Add `"pathspec>=1.1.1,<2"` to `pyproject.toml` and `pathspec>=1.1.1,<2` to the data-generation image requirements.

- [ ] **Step 2: Install the declared package in the pane environment**

Install the code-understanding package in the pane's isolated environment so the following tests exercise the declared dependency.

- [ ] **Step 3: Write failing policy-loading tests**

Add `TestRepositoryIgnorePolicyLoading` cases named:

```python
test_missing_file_creates_empty_policy
test_empty_and_comment_only_files_have_zero_active_patterns
test_only_root_augurignore_is_loaded
test_crlf_unicode_and_missing_final_newline_are_accepted
test_invalid_utf8_raises_repository_ignore_error
test_matcher_compile_error_names_file_without_dumping_contents
test_loading_logs_missing_file_or_active_pattern_count
```

Assert the exact filename, `active_pattern_count`, `has_patterns`, and `RepositoryIgnoreError` behavior. For the compile-error case, patch `GitIgnoreSpec.from_lines` to raise so the wrapper behavior does not depend on which malformed patterns a particular Git/pathspec version treats as non-matching.

- [ ] **Step 4: Run loading tests and confirm the expected failure**

Run from `workflows/examples/code_understanding`:

```bash
python3 -m unittest \
  tests.test_repository_ignore.TestRepositoryIgnorePolicyLoading -v
```

Expected: FAIL because `utils.repository_ignore` does not exist.

- [ ] **Step 5: Implement loading, validation, and the public data types**

Implement the Task 1 interfaces in `utils/repository_ignore.py`. Store the repository root as an absolute normalized path, open only its direct `.augurignore` child with UTF-8, compile once, count active non-comment patterns, and wrap read/decode/compile exceptions in `RepositoryIgnoreError` without including pattern contents. Log either that no root file was found or the loaded active-pattern count at normal log level.

- [ ] **Step 6: Run loading tests to green**

Run the Step 4 command. Expected: all loading tests PASS.

- [ ] **Step 7: Write failing matching, filtering, and statistics tests**

Add `TestRepositoryIgnorePolicyMatching` cases for comments, escaped `#`/`!`, escaped trailing spaces, `*`, `?`, ranges, `**`, root anchoring, directory-only rules, last-match-wins, valid negation, excluded-parent behavior, separator normalization, and out-of-root rejection. Add these focused assertions:

```python
self.assertEqual(
    policy.filter_directories(root, ["vendor", "cache", "src"], built_in_names={"vendor"}),
    ["src"],
)
self.assertEqual(
    policy.filter_files(root, [".augurignore", "debug.log", "app.py"],
                        built_in_names={".augurignore"}),
    ["app.py"],
)
self.assertEqual(policy.stats, IgnoreStats(ignored_directories=2, ignored_files=2))
```

Repeat a filtering call and assert the counts remain unique rather than increasing for the same paths. Include `!vendor/keep.py` and prove it cannot override the supplied built-in `vendor` exclusion.

Add `test_individual_ignored_paths_are_debug_only`: capture logs at normal level and assert paths are absent, then capture debug logs and assert the ignored relative path is present.

- [ ] **Step 8: Run matching tests and confirm the expected failure**

```bash
python3 -m unittest \
  tests.test_repository_ignore.TestRepositoryIgnorePolicyMatching -v
```

Expected: FAIL because matching/filtering behavior is not implemented.

- [ ] **Step 9: Implement path matching, filtering, unique counters, and summary logging**

Normalize candidates to repository-relative POSIX paths, append `/` for directory identity checks, and reject candidates outside the repository root. Apply built-in names as an OR condition before custom rules. Return filtered lists without mutating caller input, track unique relative paths in private sets, log individual excluded relative paths only at debug level, and log the spec's aggregate summary from the stats view.

- [ ] **Step 10: Add the Git conformance test**

Add `TestRepositoryIgnoreGitConformance.test_representative_patterns_match_git_check_ignore`. Build a temporary tree, write identical rules to `.augurignore` and `.gitignore`, and compare policy decisions for files and directories with batched `git check-ignore --no-index --stdin`. Include root anchoring, `**`, directory rules, last-match-wins, and an excluded parent with a negated child.

- [ ] **Step 11: Run the complete policy suite**

```bash
python3 -m unittest tests.test_repository_ignore -v
```

Expected: all tests PASS, including Git conformance.

- [ ] **Step 12: Review dependency and implementation simplicity**

Confirm there is one parser, one normalization path, no custom glob engine, no global mutable policy, and no dependency on Git at runtime. Run:

```bash
git diff --check
```

Expected: no output and exit 0.

- [ ] **Step 13: Commit Task 1**

```bash
git add \
  workflows/examples/code_understanding/utils/repository_ignore.py \
  workflows/examples/code_understanding/tests/test_repository_ignore.py \
  workflows/examples/code_understanding/pyproject.toml \
  resources/images/data-generation/requirements.txt
git commit -m "feat: add augurignore repository policy"
```

Stop and send the commit hash plus Step 11 output to the orchestrator. Do not begin another task until the orchestrator accepts or returns findings.

### Task 2: Apply the Policy to Base Repository Ingestion

**Owner:** `augurignore-pipeline`

**Prerequisite:** The orchestrator has accepted Task 1 and incorporated its commit into this pane's worktree.

**Files:**
- Create: `workflows/examples/code_understanding/tests/test_augurignore_data_generation.py`
- Modify: `workflows/examples/code_understanding/utils/code_utils.py:123-173`
- Modify: `workflows/examples/code_understanding/pipelines/base/data_generation.py:13-43,92-148,275-299,477-510,594-684`

**Interfaces:**
- Consumes: all Task 1 `RepositoryIgnorePolicy` and `RepositoryIgnoreError` interfaces.
- Produces: `get_detected_languages_for_repo(code_dir: str, ignore_policy: RepositoryIgnorePolicy | None = None) -> list[str]`.
- Produces: `generate_raw_dataset(..., multi_repo: bool = False, ignore_policy: RepositoryIgnorePolicy | None = None)` with existing return behavior.
- Produces: `load_external_data(source_path: str, ignore_policy: RepositoryIgnorePolicy | None = None) -> dict`.
- Produces: `generate_code_and_meta(..., external_metadata: dict | None = None, ignore_policy: RepositoryIgnorePolicy | None = None)` with existing return behavior.
- Produces: `detect_languages(source_path: str, ignore_policy: RepositoryIgnorePolicy | None = None) -> list`.

- [ ] **Step 1: Write failing traversal integration tests**

Create `test_augurignore_data_generation.py` using temporary repository roots and `unittest.mock`. Add cases named:

```python
test_language_detection_prunes_ignored_directories_and_files
test_raw_dataset_never_stats_or_opens_ignored_files
test_external_metadata_skips_ignored_json_and_prunes_ignored_directories
test_absent_augurignore_preserves_existing_builtin_exclusions
test_one_policy_is_reused_across_language_and_config_passes
test_separate_repositories_do_not_share_rules_or_stats
test_all_analyzable_content_excluded_raises_clear_error
```

Patch lexer and dataframe dependencies narrowly. For the content-read test, load the policy before installing the `open` spy, then assert ignored paths never reach `getsize()` or `open()`. For `.code_metadata`, place kept and ignored JSON files in separate subdirectories and assert only kept keys are merged.

- [ ] **Step 2: Run the new integration suite and confirm failure**

```bash
cd workflows/examples/code_understanding
python3 -m unittest tests.test_augurignore_data_generation -v
```

Expected: FAIL because the ingestion functions do not accept or apply the policy.

- [ ] **Step 3: Remove the unrestricted post-clone file enumeration**

Delete the `all_files = [...]` `os.walk()` and its debug log from `clone_from_repo`. Retain clone success and credential-redacted failure logging.

- [ ] **Step 4: Apply directory and file filtering during language detection**

Update `get_detected_languages_for_repo` to use the supplied policy, or load one from `code_dir` only when direct legacy callers omit it. Prune directories before descent and filter filenames before lexer detection. Keep existing language thresholds and mappings unchanged.

- [ ] **Step 5: Apply filtering during raw dataset and external metadata reads**

Update `generate_raw_dataset` so existing language-specific excluded directories are passed as `built_in_names`, `.augurignore` is excluded from root file candidates, and custom filtering occurs before extension checks, size checks, or file opens. Update `load_external_data` to prune and filter within `.code_metadata` without applying the source-ingestion built-in exclusion that intentionally hides the `.code_metadata` directory itself.

- [ ] **Step 6: Create and thread one policy through the base pipeline**

After `prepare_environment` clones the repository, create one `RepositoryIgnorePolicy` and pass it to `detect_languages`, `load_external_data`, every `generate_code_and_meta` call, and ultimately every `generate_raw_dataset` call. Replace the duplicated tracker/no-tracker processing branches in `DataGenerationPipeline.run` with one `nullcontext()`-backed flow while preserving timing labels and result/error behavior.

When language detection finds nothing and the policy has active patterns, raise `RepositoryIgnoreError` with a message stating that no supported source or configuration files remain after exclusions. Preserve the existing no-language error for repositories without active custom patterns. Log the policy summary once per repository run.

- [ ] **Step 7: Run Task 2 tests**

```bash
cd workflows/examples/code_understanding
python3 -m unittest \
  tests.test_repository_ignore \
  tests.test_augurignore_data_generation -v
```

Expected: all tests PASS.

- [ ] **Step 8: Run existing data-generation telemetry regression tests**

```bash
cd workflows/examples/code_understanding
python3 -m unittest tests.test_data_generation_telemetry -v
```

Expected: all tests PASS with existing timing/token semantics intact.

- [ ] **Step 9: Inspect for duplicate policy loads or matching branches**

Confirm the normal base pipeline compiles one policy, passes it explicitly, and has one repository-processing control flow. Run `git diff --check`; expected: no output and exit 0.

- [ ] **Step 10: Commit Task 2**

```bash
git add \
  workflows/examples/code_understanding/tests/test_augurignore_data_generation.py \
  workflows/examples/code_understanding/utils/code_utils.py \
  workflows/examples/code_understanding/pipelines/base/data_generation.py
git commit -m "feat: apply augurignore during ingestion"
```

Stop and send the commit hash plus Steps 7-8 output to the orchestrator.

### Task 3: Kubeflow Wiring and Failure Propagation

**Owner:** `augurignore-pipeline`

**Prerequisite:** The orchestrator has accepted Task 2.

**Files:**
- Modify: `workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py:74-203`
- Modify: `workflows/examples/code_understanding/pipelines/base/data_generation.py`
- Modify: `workflows/examples/code_understanding/tests/test_augurignore_data_generation.py`

**Interfaces:**
- Consumes: Task 1 policy and Task 2 ingestion signatures.
- Produces: `should_reraise_processing_error(error: Exception, *, multi_repo: bool) -> bool`, returning `True` for every `RepositoryIgnoreError` and for all errors in single-repository mode, while leaving unrelated multi-repository errors skippable.
- Produces: Kubeflow wiring that passes one extracted-repository policy through all Task 2 calls.

- [ ] **Step 1: Add a failing Kubeflow failure-classification regression test**

Add class `TestAugurIgnoreKubeflowFailure` with `test_repository_ignore_error_is_fatal_in_multi_repo_mode` and `test_unrelated_error_remains_skippable_in_multi_repo_mode`. Exercise the `should_reraise_processing_error` interface directly so this behavior is testable without importing KFP. Assert that `RepositoryIgnoreError` returns `True` when `multi_repo=True`, an unrelated error returns `False` when `multi_repo=True`, and every error returns `True` when `multi_repo=False`.

- [ ] **Step 2: Run the focused regression test and confirm failure**

```bash
cd workflows/examples/code_understanding
python3 -m unittest \
  tests.test_augurignore_data_generation.TestAugurIgnoreKubeflowFailure -v
```

Expected: FAIL because `should_reraise_processing_error` does not exist.

- [ ] **Step 3: Load and share the policy in the Kubeflow component**

Inside `generate_code_and_meta_op`, create one `RepositoryIgnorePolicy` immediately after extracting `source_dir`, before repository analysis. Pass it to `load_external_data`, `detect_languages`, and `generate_code_and_meta`. Log its aggregate summary from the component's completion path.

- [ ] **Step 4: Preserve repository-ignore failures through multi-repo handling**

Implement `should_reraise_processing_error` in the base module and call it from the Kubeflow generic exception branch. Preserve the preceding rate-limit-specific logging and re-raise behavior. Use the helper to re-raise repository-ignore errors after repository-safe logging while retaining the existing skip behavior for unrelated multi-repository errors. Ensure the all-content-excluded error follows this same fatal path.

- [ ] **Step 5: Run policy, ingestion, and failure tests**

```bash
cd workflows/examples/code_understanding
python3 -m unittest \
  tests.test_repository_ignore \
  tests.test_augurignore_data_generation -v
```

Expected: all tests PASS.

- [ ] **Step 6: Compile every Kubeflow pipeline definition**

```bash
cd workflows/examples/code_understanding
rm -rf /tmp/augurignore-compiled-pipelines
PIPELINE_COMPILE_ONLY=1 \
KFP_PIPELINE_OUTPUT_DIR=/tmp/augurignore-compiled-pipelines \
PYTHONPATH=. \
python3 pipelines/orchestrator.py
```

Expected: exit 0 and YAML outputs for `data_generation`, `single_repo`, `multi_repo`, `indexing`, and `analysis`. The temporary output remains outside the repository and is not committed.

- [ ] **Step 7: Commit Task 3**

```bash
git add \
  workflows/examples/code_understanding/pipelines/base/data_generation.py \
  workflows/examples/code_understanding/pipelines/kubeflow/data_generation.py \
  workflows/examples/code_understanding/tests/test_augurignore_data_generation.py
git commit -m "feat: enforce augurignore in kubeflow"
```

Stop and send the commit hash plus Steps 5-6 output to the orchestrator.

### Task 4: User Documentation

**Owner:** `augurignore-policy`

**Files:**
- Modify: `workflows/examples/code_understanding/README.md:1-25`

**Interfaces:**
- Consumes: the approved behavior in the spec and Tasks 1-3.
- Produces: user-facing instructions; no code interface.

- [ ] **Step 1: Add the `.augurignore` documentation section**

Document all of the following without adding client-side or nested-file behavior:

- Exact root filename and client commit requirement.
- Optional-file behavior.
- Git-compatible semantics and `pathspec` implementation note.
- Additive relationship to built-in exclusions and non-overridable built-ins.
- Clone/download limitation.
- Fail-fast behavior for a present invalid file.
- Debug-versus-normal logging behavior.
- The exact example from the design spec.

- [ ] **Step 2: Verify documentation against the shipped interface**

Search for misspellings and unsupported claims:

```bash
rg -n "\\.agignore|\\.augerignore|nested|runtime patterns" \
  workflows/examples/code_understanding/README.md
git diff --check
```

Expected: no filename misspellings, any uses of “nested” or “runtime patterns” explicitly state they are unsupported, and `git diff --check` exits 0.

- [ ] **Step 3: Commit Task 4**

```bash
git add workflows/examples/code_understanding/README.md
git commit -m "docs: document augurignore configuration"
```

Stop and send the commit hash plus Step 2 output to the orchestrator.

## Orchestrator Final Verification

After integrating all accepted task commits, the primary pane performs these checks before asking for the xhigh review:

- [ ] Confirm `git status --short` contains no unexpected files and preserves the user's pre-existing untracked `docs/assumptions.md`.
- [ ] Run the focused suites:

```bash
cd workflows/examples/code_understanding
python3 -m unittest \
  tests.test_repository_ignore \
  tests.test_augurignore_data_generation -v
```

- [ ] Run the complete existing unit suite:

```bash
cd workflows/examples/code_understanding
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

- [ ] Recompile all pipeline definitions using Task 3 Step 6.
- [ ] Run `git diff --check` and inspect the combined diff for duplicated traversal logic, repeated policy construction, broad exception handling, accidental reads before filtering, and documentation drift.
- [ ] Prompt `augurignore-review` at `gpt-5.6-sol` xhigh to review the complete branch against the spec and plan, focusing on correctness, Git-semantic gaps, error propagation, tests, unnecessary complexity, simplification opportunities, and documentation accuracy.
- [ ] Route each actionable finding to the owning pane, review its correction commit, rerun the affected focused tests and the complete suite, and request a final xhigh re-check for material changes.
- [ ] Do not claim completion until the orchestrator has current passing output from the full unit suite and pipeline compilation, and the xhigh reviewer has no unresolved actionable findings.
