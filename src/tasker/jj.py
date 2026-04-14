"""Backward-compatible re-exports from the new VCS module.

The jj integration has moved to ``tasker.vcs.jj_backend``.
This module re-exports the public API for backward compatibility
with existing code and tests that import from ``tasker.jj``.
"""

from __future__ import annotations

from .vcs.jj_backend import (
    JJBackend,
    JJResult,
    _run_jj,
    jj_commit_task,
    jj_diff,
    jj_get_current_change_id,
    jj_has_changes,
    jj_is_available,
    jj_log,
    jj_new_task,
)

__all__ = [
    "JJBackend",
    "JJResult",
    "_run_jj",
    "jj_commit_task",
    "jj_diff",
    "jj_get_current_change_id",
    "jj_has_changes",
    "jj_is_available",
    "jj_log",
    "jj_new_task",
]
