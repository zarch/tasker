"""VCS backend abstraction for tasker.

Provides a protocol that both jj and git backends implement,
allowing the orchestrator to work with either version control system.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Task


@runtime_checkable
class VCSBackend(Protocol):
    """Protocol for version control backends (jj, git).

    Each backend manages task-scoped changes so the orchestrator gets
    clean diffs for QA review and atomic commits on approval.
    """

    def is_available(self) -> bool:
        """Check if the VCS tool is available in PATH."""
        ...

    def init(self, cwd: str | None = None) -> None:
        """Initialize the VCS state for the first task.

        Called once at orchestrator startup. Captures the current
        state (e.g. current jj change, current git HEAD) so that
        subsequent tasks can branch from the right point.
        """
        ...

    def begin_task(self, task: Task, cwd: str | None = None) -> None:
        """Create an isolated workspace for a task.

        Sets task.base_ref and task.task_ref with VCS-specific
        identifiers (change IDs for jj, branch names for git).
        """
        ...

    def get_diff(self, task: Task, cwd: str | None = None) -> str:
        """Return the diff for the current task's changes.

        Used to inject context into QA prompts.
        """
        ...

    def commit_task(self, task: Task, cwd: str | None = None) -> None:
        """Finalize the task's changes as a permanent commit.

        Called when QA approves (or user skips a blocked task).
        Updates internal state so the next task branches from here.
        """
        ...


def create_backend(vcs_type: str) -> VCSBackend | None:
    """Factory: create a VCS backend from a CLI flag value.

    Args:
        vcs_type: "jj", "git", or "none".

    Returns:
        A VCSBackend instance, or None when vcs_type is "none".
    """
    if vcs_type == "none":
        return None
    if vcs_type == "jj":
        from .jj_backend import JJBackend

        return JJBackend()
    if vcs_type == "git":
        from .git_backend import GitBackend

        return GitBackend()
    raise ValueError(f"Unknown VCS type: {vcs_type!r}. Must be 'jj', 'git', or 'none'.")
