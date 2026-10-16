"""Bounded, metadata-only observations of an attempted MLflow payload.

Counts are regular-file logical sizes at scan time, not network/persisted bytes.
Symlinks are not followed. A partial scan is a lower bound. No file is opened.
"""
import hashlib
import os
import stat
import threading
import time
import uuid
from collections import OrderedDict

_salt = uuid.uuid4().bytes
_ordinals = OrderedDict()
_lock = threading.Lock()
_MAX_KEYS = 256
_MAX_ENTRIES = 10000
_MAX_SECONDS = 0.25
_MAX_DEPTH = 64


def upload_identity(path, artifact_path=None):
    """Process-local opaque key + ordinal; raw paths never leave this helper."""
    try:
        value = os.path.abspath(os.fspath(path)) + "\0" + (artifact_path or "")
        key = hashlib.blake2b(value.encode("utf-8", "surrogatepass"), key=_salt, digest_size=16).hexdigest()
        with _lock:
            ordinal = _ordinals.pop(key, 0) + 1
            _ordinals[key] = ordinal
            if len(_ordinals) > _MAX_KEYS:
                _ordinals.popitem(last=False)
        return {"payload_key": key, "upload_ordinal": ordinal}
    except Exception:
        return {}


def measure_payload(path):
    """Scan at most 10k entries/250ms; a blocking filesystem call can exceed it."""
    result = {"payload_regular_files": 0, "payload_logical_bytes": 0,
              "payload_scan_entries": 0, "payload_scan_errors": 0,
              "payload_symlinks": 0, "payload_scan_complete": True,
              "payload_scan_truncated": False}
    start = time.perf_counter()
    stack = [path]
    iterators = []
    try:
        while stack or iterators:
            if result["payload_scan_entries"] >= _MAX_ENTRIES or time.perf_counter() - start >= _MAX_SECONDS:
                result["payload_scan_truncated"] = True
                result["payload_scan_complete"] = False
                break
            if stack:
                current = stack.pop()
                result["payload_scan_entries"] += 1
                try:
                    info = os.lstat(current)
                except OSError:
                    result["payload_scan_errors"] += 1
                    result["payload_scan_complete"] = False
                    continue
            else:
                try:
                    entry = next(iterators[-1])
                except StopIteration:
                    iterators.pop().close()
                    continue
                except OSError:
                    iterators.pop().close()
                    result["payload_scan_errors"] += 1
                    result["payload_scan_complete"] = False
                    continue
                current = entry.path
                result["payload_scan_entries"] += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    result["payload_scan_errors"] += 1
                    result["payload_scan_complete"] = False
                    continue
            if stat.S_ISLNK(info.st_mode):
                result["payload_symlinks"] += 1
                result["payload_scan_complete"] = False
            elif stat.S_ISREG(info.st_mode):
                result["payload_regular_files"] += 1
                result["payload_logical_bytes"] += info.st_size
            elif stat.S_ISDIR(info.st_mode):
                if len(iterators) >= _MAX_DEPTH:
                    result["payload_scan_complete"] = False
                    result["payload_scan_truncated"] = True
                    continue
                try:
                    iterators.append(os.scandir(current))
                except OSError:
                    result["payload_scan_errors"] += 1
                    result["payload_scan_complete"] = False
            else:
                result["payload_scan_complete"] = False
    except Exception:
        result["payload_scan_errors"] += 1
        result["payload_scan_complete"] = False
    finally:
        for iterator in iterators:
            try:
                iterator.close()
            except Exception:
                pass
    return result
