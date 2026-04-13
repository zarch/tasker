"""Jujutsu (jj) VCS integration — manages task-scoped changes.

All commands use `-m` for non-interactive operation (never opens $EDITOR).

Workflow (Option A — single commit per task):
    1. jj_new_task(parent, description)   → creates isolated change for the task
    2. Developer writes code (no jj commit — stays in working copy)
    3. jj_diff(base_change)               → QA reviews all changes
    4. jj_commit_task()                   → finalize on QA approval
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
        return JJResult(success=False, stdout="", stderr=f"TIMEOUT after {timeout_secs}s", return_code=-1)
    except FileNotFoundError:
        return JJResult(success=False, stdout="", stderr="jj not found in PATH", return_code=-1)


def jj_is_available() -> bool:
    """Check if jj CLI is available."""
    result = _run_jj(["version"])
    return result.success


def jj_new_task(
    parent_change_id: str,
    description: str,
    cwd: Path | None = None,
) -> JJResult:
    """Create a new empty change for a task.

    This is the key operation: it creates an isolated workspace where
    the developer can work without affecting other tasks.

    Args:
        parent_change_id: The change ID to base the new task on (e.g., last committed task).
        description: The task description (used as jj commit message).
        cwd: Working directory for the jj repo.

    Returns:
        JJResult with the new change's info.
    """
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
    """Commit the current working-copy change (finalize task on QA approval).

    This is equivalent to `jj describe -m <msg>` + `jj new`.
    After this, the task's work is a permanent commit and @ is a new empty change.

    Args:
        description: The commit message (should be the task description).
        cwd: Working directory for the jj repo.

    Returns:
        JJResult with commit info.
    """
    result = _run_jj(
        ["commit", "-m", description],
        cwd=cwd,
    )
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
    """Get the diff from a base change to the working copy.

    This shows ALL changes for the current task, regardless of any
    intermediate checkpoints.

    Args:
        base_change_id: The change ID to diff from (task's parent).
        cwd: Working directory for the jj repo.
        stat: If True, show file-level stats instead of full diff.

    Returns:
        JJResult with the diff output.
    """
    args = ["diff", "--from", base_change_id]
    if stat:
        args.append("--stat")
    result = _run_jj(args, cwd=cwd)
    return result


def jj_get_current_change_id(cwd: Path | None = None) -> str | None:
    """Get the change ID of the current working copy (@).

    Returns:
        The change ID string, or None if jj is not available.
    """
    result = _run_jj(
        ["log", "--no-graph", "-T", "change_id", "-r", "@"],
        cwd=cwd,
    )
    if result.success and result.stdout.strip():
        return result.stdout.strip()
    return None


def jj_log(cwd: Path | None = None, limit: int = 5) -> str:
    """Get a compact log of recent changes.

    Returns:
        String with the jj log output.
    """
    result = _run_jj(
        ["log", "--limit", str(limit)],
        cwd=cwd,
    )
    return result.stdout if result.success else f"(error: {result.stderr})"


def jj_has_changes(cwd: Path | None = None) -> bool:
    """Check if there are any uncommitted changes in the working copy.

    Returns:
        True if the working copy has modifications.
    """
    # Check if the current change has any parent diff
    result = _run_jj(
        ["diff", "--stat"],
        cwd=cwd,
    )
    if not result.success:
        return False
    # If there's any output, there are changes
    return bool(result.stdout.strip())
