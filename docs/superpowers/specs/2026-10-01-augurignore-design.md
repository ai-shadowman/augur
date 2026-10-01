# `.augurignore` Repository Exclusions Design

## Summary

Augur will support an optional `.augurignore` file at the root of each analyzed repository. The file will use Git-compatible ignore semantics and will add repository-specific exclusions to Augur's existing built-in exclusions.

The client is responsible for creating or updating `.augurignore` and committing it to the remote repository before Augur runs. Augur will use only the file present on the selected remote branch; it will not accept additional ignore patterns through runtime or client configuration.

## Goals

- Let repository owners prevent selected files and directories from entering Augur's analysis pipeline.
- Apply one consistent ignore policy to all repository-reading stages.
- Provide familiar `.gitignore` behavior, including ordered rules and negation.
- Fail before analysis when a present `.augurignore` cannot be safely applied.
- Preserve Augur's existing built-in exclusions.
- Validate matcher behavior against Git itself.

## Non-Goals

- Creating or committing `.augurignore` from Augur.
- Accepting ignore patterns through pipeline parameters, environment variables, or other runtime configuration.
- Reading nested `.augurignore` files.
- Allowing `.augurignore` rules to override Augur's built-in exclusions.
- Preventing the initial Git clone or checkout from downloading ignored content.
- Changing the set of languages or file extensions Augur supports.

## User-Facing Contract

### Location and lifecycle

Augur will look only for `<repository-root>/.augurignore` after cloning the selected branch. A nested file with the same name has no configuration meaning.

The ignore file cannot affect cloning because Augur must first fetch and check out the repository to discover it. Once the clone is complete, Augur will load and validate `.augurignore` before language detection, metadata loading, source collection, or any other analysis traversal. Except for loading `.augurignore` itself, ignored file contents will not be opened, analyzed, sent to an LLM, or written to generated datasets.

### Optional file behavior

If `.augurignore` is absent, Augur will apply no repository-specific ignore rules and will retain its current built-in exclusions. An empty or comment-only file behaves the same way.

If `.augurignore` exists but cannot be read, decoded as UTF-8, or compiled, the repository's data-generation run will fail before analysis begins. Augur must not silently continue without rules the repository owner intended to enforce.

### Pattern behavior

Patterns will follow the behavior documented for `.gitignore`, including:

- Blank lines and comments.
- Escaped leading `#` and `!` characters.
- Trailing-space escaping.
- `*`, `?`, character ranges, and `**` wildcards.
- Root-relative patterns beginning with `/`.
- Directory-only patterns ending with `/`.
- Ordered evaluation where the last matching rule decides the result.
- Negation with `!`.
- Git's excluded-parent constraint: a rule cannot re-include a file beneath an excluded parent directory.

All candidate paths will be normalized to repository-relative paths with `/` separators before matching.

### Interaction with built-in exclusions

`.augurignore` adds exclusions on top of Augur's existing built-in rules. A path is excluded when either the applicable built-in policy or `.augurignore` excludes it.

Negation affects only earlier `.augurignore` rules. It cannot re-include paths excluded by Augur's built-in policy.

## Architecture

### RepositoryIgnorePolicy

A new focused module, expected at `utils/repository_ignore.py`, will own `.augurignore` behavior. It will expose a compiled `RepositoryIgnorePolicy` for one repository root.

The component will:

1. Locate only the root `.augurignore`.
2. Read it as UTF-8.
3. Compile it once per repository-processing execution context with `pathspec.GitIgnoreSpec`.
4. Normalize candidate paths relative to the repository root.
5. Decide whether a directory may be traversed.
6. Decide whether a file may be opened or processed.
7. Combine custom results with built-in exclusions supplied by the caller.

The public interface must distinguish directory checks from file checks. Directory paths need directory-aware matching so that a trailing-slash rule prunes traversal exactly when expected.

The policy will be passed explicitly to consumers. It will not use module-level mutable state, environment variables, or a process-wide singleton. Explicit ownership prevents rules from leaking between repositories during multi-repository execution and makes the component independently testable.

### Data flow

The repository processing sequence will be:

```text
clone selected branch
        |
        v
load and validate root .augurignore
        |
        v
shared RepositoryIgnorePolicy
        |-- language detection
        |-- .code_metadata loading
        `-- source/config dataset generation
```

Every `os.walk()` over cloned repository content will prune excluded directory names before descent. Each candidate file will be checked again immediately before opening. The current debug-only traversal that enumerates every file after clone will be removed or made subject to the same policy.

The main integration points are:

- `utils/code_utils.py`: language detection must accept and apply the policy.
- `pipelines/base/data_generation.py`: repository preparation, external metadata loading, and raw dataset generation must share the same policy instance.
- `pipelines/kubeflow/data_generation.py`: after the cloned repository artifact is extracted in the data-generation component, that component must load one policy instance and share it across its repository-reading operations. Failures must surface through the Kubeflow task.

The exact function signatures may follow existing project conventions, but no repository-reading caller may independently parse `.augurignore` or reimplement matching.

### Dependency

Augur will declare a compatible `pathspec` 1.x dependency explicitly in the installable code-understanding package. The project will use `pathspec.GitIgnoreSpec`, not generic `fnmatch` or a custom parser, because it is designed to reproduce Git ignore edge cases.

The dependency is MPL-2.0 licensed and must pass the project's normal dependency and license review. Runtime images and direct local installation must both receive the dependency through the project's declared packaging path rather than relying on a transitive installation.

## Error Handling

A dedicated repository-ignore configuration error will identify the affected repository and provide a safe explanation. When available, the message should include the line number and parsing problem, but it must not print repository credentials or the entire `.augurignore` file.

Failure cases include:

- The root `.augurignore` exists but cannot be opened.
- The file is not valid UTF-8.
- A pattern cannot be compiled.
- No supported source or configuration files remain after built-in and custom exclusions are applied.

Patterns that are valid but match nothing are not errors.

The error will follow the execution environment's existing fatal-error path. A local single-repository run will return its normal error result. In Kubeflow, the affected repository task will fail visibly instead of silently processing the repository without its exclusions. This feature will not introduce a new cross-repository orchestration or retry policy.

## Observability

Normal logs will state whether `.augurignore` was found and, when present, the number of active compiled patterns. After traversal, Augur will log aggregate counts of ignored directories and files.

Example:

```text
Loaded .augurignore: 8 active patterns
Repository filtering: 4 directories and 27 files ignored
```

Individual ignored paths will be emitted only at debug level to avoid noisy logs and unnecessary disclosure of repository structure.

## Testing and Validation

### Policy unit tests

Unit tests will cover:

- Missing, empty, and comment-only files.
- Comments and escaped leading characters.
- Escaped trailing spaces.
- `*`, `?`, ranges, and `**`.
- Root anchoring.
- Directory-only rules.
- Last-match-wins ordering.
- Valid negation.
- The excluded-parent constraint.
- Built-in exclusions resisting negation.
- Root-only discovery.
- UTF-8 decoding and malformed-pattern failures.
- Repository-relative and platform-specific path normalization.

### Git conformance tests

A conformance test will create a temporary repository tree and write the same patterns to `.augurignore` for Augur and `.gitignore` for Git. Augur's per-path decisions will be compared with `git check-ignore --no-index`, including files that would otherwise be considered tracked. This anchors the supported behavior to Git's implementation rather than only to hand-written expectations.

The suite will include the documented constraint that a file cannot be re-included beneath a pruned parent directory.

### Pipeline integration tests

Integration tests will verify that:

- Excluded directories are pruned before descent.
- Excluded files are not opened.
- Language detection and dataset generation apply the same custom rules.
- `.code_metadata` loading respects `.augurignore`.
- Separate repositories receive separate policy instances.
- Excluding all analyzable content produces the intended error.
- Base pipeline imports continue to work.
- Kubeflow pipeline definitions still import and compile.

## Documentation

The code-understanding README will document:

- The exact root filename `.augurignore`.
- That the client must commit the file before Augur runs.
- The supported Git-compatible syntax.
- The additive relationship with built-in exclusions.
- Root-only behavior.
- Failure behavior for an unreadable or invalid file.
- A concise example.

Example:

```gitignore
# Generated fixtures
tests/fixtures/generated/

# Large captured payloads
**/*.har

# Ignore reports except the maintained example
reports/*
!reports/example.md
```

## Expected Change Scope

The implementation is expected to touch:

- A new `utils/repository_ignore.py` policy module.
- `utils/code_utils.py` traversal helpers.
- Base and Kubeflow data-generation pipeline integration.
- Package dependency declarations.
- Focused unit and integration tests.
- The code-understanding README.

Client-side file creation, nested ignore files, and unrelated pipeline refactoring remain outside this change.
