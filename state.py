"""
state.py — Shared state.json read/write helpers for BaseStation services.

All services read and write /run/mowerbase/state.json.
Uses fcntl file locking to prevent concurrent write corruption.
"""

import json
import os
import fcntl
import logging
from typing import Any

STATE_FILE = "/run/mowerbase/state.json"
STATE_DIR = "/run/mowerbase"

log = logging.getLogger(__name__)


def _ensure_dir() -> None:
    """Create the state directory if it doesn't exist."""
    os.makedirs(STATE_DIR, exist_ok=True)


def read_state() -> dict:
    """
    Read and return the current state dict.
    Returns empty dict if file is missing or unreadable (boot condition).
    """
    try:
        with open(STATE_FILE, "r") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        log.warning("state.py: read_state failed: %s", e)
        return {}


def update_state(patch: dict) -> None:
    """
    Merge patch into the current state and write back atomically.
    Uses an exclusive file lock to prevent concurrent write corruption.
    Writes to a temp file then renames for atomic replacement.
    """
    _ensure_dir()
    lock_path = STATE_FILE + ".lock"
    try:
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                current = read_state()
                _deep_merge(current, patch)
                tmp_path = STATE_FILE + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(current, f, indent=2, default=str)
                os.replace(tmp_path, STATE_FILE)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except OSError as e:
        log.error("state.py: update_state failed: %s", e)


def write_state(state: dict) -> None:
    """
    Overwrite state.json completely with the provided dict.
    Used by gps.py which owns the authoritative state.
    """
    _ensure_dir()
    lock_path = STATE_FILE + ".lock"
    try:
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                tmp_path = STATE_FILE + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(state, f, indent=2, default=str)
                os.replace(tmp_path, STATE_FILE)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    except OSError as e:
        log.error("state.py: write_state failed: %s", e)


def _deep_merge(base: dict, patch: dict) -> None:
    """Merge patch into base in-place. Nested dicts are merged recursively."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def get_field(key: str, default: Any = None) -> Any:
    """Convenience: read a single top-level field from state."""
    return read_state().get(key, default)
