"""Jujutsu (jj) VCS backend.

All commands use `-m` for non-interactive operation (never opens $EDITOR).

Workflow (Option A — single commit per task):
    1. init()           → captures current change as the starting base
    2. begin_task()     → creates isolated change for the task
    3. Developer writes code (no jj commit — stays in working copy)
    4. get_diff()       → QA reviews all changes
    5. commit_task()    → finalize on QA approval
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..models import Task

logger = logging.getLogger(__name__)


@dataclass
class JJResult:
    """Result of a jj command."""

    success: bool
    stdout: str
    stderr: str
    return_code: int


def _run_jj(
    args: list[str],
    cwd: Path | None = None,
    timeout_secs: int = 30,
) -> JJResult:
    """Run a jj command and return structured result.

    All jj commands MUST pass `-m` with a message to avoid opening $EDITOR.
    """
    cmd = ["jj"] + args
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            cwd=str(cwd) if cwd else None,
        )
        return JJResult(
            success=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            return_code=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return JJResult(
            success=False,
            stdout="",
            stderr=f"TIMEOUT after {timeout_secs}s",
            return_code=-1,
        )
    except FileNotFoundError:
        return JJResult(
            success=False, stdout="", stderr="jj not found in PATH", return_code=-1
        )


def _jj_get_current_change_id(cwd: Path | None = None) -> str | None:
    """Get the change ID of the current working copy (@)."""
    result = _run_jj(
        ["log", "--no-graph", "-T", "change_id", "-r", "@"],
        cwd=cwd,
    )
    if result.success and result.stdout.strip():
        return result.stdout.strip()
    return None


class JJBackend:
    """Jujutsu VCS backend — manages task-scoped changes via jj new/commit."""

    def __init__(self) -> None:
        self._last_committed_change_id: str | None = None

    # ── Protocol implementation ──────────────────────────────────

    def is_available(self) -> bool:
        """Check if jj CLI is available."""
        return _run_jj(["version"]).success

    def init(self, cwd: str | None = None) -> None:
        """Capture the current change as the starting base."""
        current = _jj_get_current_change_id(cwd=cwd)
        if current:
            self._last_committed_change_id = current
        else:
            raise RuntimeError("Could not determine current jj change ID during init.")

    def begin_task(self, task: Task, cwd: str | None = None) -> None:
        """Create a new jj change for the task."""
        if not self._last_committed_change_id:
            return

        result = _run_jj(
            ["new", self._last_committed_change_id, "-m", task.vcs_description],
            cwd=cwd,
        )

        if result.success:
            task.task_ref = _jj_get_current_change_id(cwd=cwd)
            task.base_ref = self._last_committed_change_id
            logger.info("Created new task change: %s", task.vcs_description[:60])
        else:
            logger.error("Failed to create new task change: %s", result.stderr)
            raise RuntimeError(f"jj new failed: {result.stderr}")

    def get_diff(self, task: Task, cwd: str | None = None) -> str:
        """Get the diff from the task's base change to the working copy."""
        if not task.base_ref:
            return ""
        result = _run_jj(["diff", "--from", task.base_ref], cwd=cwd)
        if result.success and result.stdout.strip():
            return result.stdout.strip()
        return ""

    def commit_task(self, task: Task, cwd: str | None = None) -> None:
        """Commit the current working-copy change (finalize task on QA approval)."""
        result = _run_jj(["commit", "-m", task.vcs_description], cwd=cwd)

        if result.success:
            # After `jj commit`, the committed change is the parent of @.
            # Update _last_committed_change_id so the next task branches from here.
            self._last_committed_change_id = task.task_ref
            logger.info("Committed task: %s", task.vcs_description[:60])
        else:
            logger.error("Failed to commit task: %s", result.stderr)
            raise RuntimeError(f"jj commit failed: {result.stderr}")


# ── Standalone helpers (used by tests and diagnostics) ────────────


def jj_is_available() -> bool:
    """Check if jj CLI is available."""
    return _run_jj(["version"]).success


def jj_new_task(
    parent_change_id: str,
    description: str,
    cwd: Path | None = None,
) -> JJResult:
    """Create a new empty change for a task."""
    result = _run_jj(
        ["new", parent_change_id, "-m", description],
        cwd=cwd,
    )
    if result.success:
        logger.info("Created new task change: %s", description[:60])
    else:
        logger.error("Failed to create new task change: %s", result.stderr)
    return result


def jj_commit_task(
    description: str,
    cwd: Path | None = None,
) -> JJResult:
    """Commit the current working-copy change."""
    result = _run_jj(["commit", "-m", description], cwd=cwd)
    if result.success:
        logger.info("Committed task: %s", description[:60])
    else:
        logger.error("Failed to commit task: %s", result.stderr)
    return result


def jj_diff(
    base_change_id: str,
    cwd: Path | None = None,
    stat: bool = False,
) -> JJResult:
    """Get the diff from a base change to the working copy."""
    args = ["diff", "--from", base_change_id]
    if stat:
        args.append("--stat")
    return _run_jj(args, cwd=cwd)


def jj_get_current_change_id(cwd: Path | None = None) -> str | None:
    """Get the change ID of the current working copy (@)."""
    return _jj_get_current_change_id(cwd=cwd)


def jj_log(cwd: Path | None = None, limit: int = 5) -> str:
    """Get a compact log of recent changes."""
    result = _run_jj(["log", "--limit", str(limit)], cwd=cwd)
    return result.stdout if result.success else f"(error: {result.stderr})"


def jj_has_changes(cwd: Path | None = None) -> bool:
    """Check if there are any uncommitted changes in the working copy."""
    result = _run_jj(["diff", "--stat"], cwd=cwd)
    if not result.success:
        return False
    return bool(result.stdout.strip())
