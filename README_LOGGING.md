# AUGUR Logging Architecture

This document describes the unified console logging model used across AUGUR.

---

## Overview

AUGUR enforces a **console-only** logging strategy:
- All log records stream directly to `sys.stdout` (console).
- No log files are written to disk.
- Third-party or library `FileHandler` instances are automatically closed and purged.

This ensures native compatibility with container engines (Kubernetes, Red Hat OpenShift AI, Kubeflow Pipelines, Podman, Docker) where stdout is collected directly by platform logging drivers.

---

## Configuration

Set the logging verbosity via the single `LOGLEVEL` environment variable in `.env` or in container specifications:

```bash
LOGLEVEL=INFO  # Options: DEBUG, INFO, WARNING, ERROR (case-insensitive)
```

| Level | Description |
|---|---|
| `DEBUG` | Verbose diagnostic details, internal step transitions, and raw traces. |
| `INFO` | Default operational logs, pipeline progress, and summary metrics. |
| `WARNING` | Recoverable issues, fallbacks, and missing optional environment variables. |
| `ERROR` | Pipeline failures, execution errors, and termination events. |

---

## Log Format

All output is consistently formatted:

```text
[%(levelname)s] %(asctime)s - %(name)s - %(message)s
```

Example console output:
```text
[INFO] 2026-09-23 08:57:48,997 - workflows.examples.code_understanding.pipelines.base.indexing - Preparing GraphRAG index
[DEBUG] 2026-09-23 08:57:48,998 - utils.duration_tracker - Recording step 'Prepare Config' duration 0.12s
```

---

## Usage in Code

To log in any module or pipeline component, import `get_logger`:

```python
from utils.kubeflow_utils import get_logger

logger = get_logger(__name__)

logger.info("Starting analysis stage")
logger.debug("Inspecting configuration details")
```

To configure or reset root logging at an application entry point:

```python
from utils.kubeflow_utils import setup_logging

# Automatically reads LOGLEVEL from environment, forces sys.stdout, and cleans any FileHandler
setup_logging()
```

---

## Verification

Run the logging test suite to verify stdout routing, handler cleanup, and level controls:

```bash
python -m unittest workflows/examples/code_understanding/tests/test_logging.py
```
