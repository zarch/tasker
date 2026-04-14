"""Git VCS backend — feature branch per task with squash merge on approval.

Workflow (Option A — single commit per task):
    1. init()           → captures current HEAD as the starting base
    2. begin_task()     → creates and checks out a feature branch (task/P1-3.T4)
    3. Developer writes code (commits freely on the branch)
    4. get_diff()       → QA reviews diff against base branch
    5. commit_task()    → squash-merges feature branch onto base, one clean commit

The developer can commit as many times as they want on the feature branch.
On QA approval, all those commits are squashed into a single clean commit
on the base branch, keeping the history tidy.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..models import Task

logger = logging.getLogger(__name__)

# Branch name prefix for task-scoped feature branches
_BRANCH_PREFIX = "task/"

# Reserved branch names that we must never use
_RESERVED_BRANCHES = {"HEAD", "main", "master", "develop", "staging"}


def _sanitize_branch_name(name: str) -> str:
    """Sanitize a task label into a valid git branch name.

    Replaces characters that are invalid in git branch names.
    See: https://git-scm.com/docs/git-check-ref-format
    """
    # Replace characters that are problematic in branch names
    sanitized = re.sub(r"[~^:\s\\]+", "-", name)
    # Remove leading dots, dashes, or slashes
    sanitized = sanitized.lstrip(".-/")
    # Collapse consecutive slashes
    sanitized = re.sub(r"/+", "/", sanitized)
    # Truncate to reasonable length (git allows 255 but let's be safe)
    return sanitized[:200]


@dataclass
class GitResult:
    """Result of a git command."""

    success: bool
    stdout: str
    stderr: str
    return_code: int


def _run_git(
    args: list[str],
    cwd: Path | None = None,
    timeout_secs: int = 30,
) -> GitResult:
    """Run a git command and return structured result."""
    cmd = ["git"] + args
    logger.debug("Running: %s (cwd=%s)", " ".join(cmd), cwd)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_secs,
            cwd=str(cwd) if cwd else None,
        )
        return GitResult(
            success=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            return_code=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return GitResult(
            success=False,
            stdout="",
            stderr=f"TIMEOUT after {timeout_secs}s",
            return_code=-1,
        )
    except FileNotFoundError:
        return GitResult(
            success=False, stdout="", stderr="git not found in PATH", return_code=-1
        )


def _get_current_branch(cwd: Path | None = None) -> str | None:
    """Get the name of the current git branch."""
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if result.success and result.stdout.strip():
        return result.stdout.strip()
    return None


def _get_commit_hash(ref: str = "HEAD", cwd: Path | None = None) -> str | None:
    """Get the commit hash for a ref."""
    result = _run_git(["rev-parse", ref], cwd=cwd)
    if result.success and result.stdout.strip():
        return result.stdout.strip()
    return None


def _is_clean_working_tree(cwd: Path | None = None) -> bool:
    """Check if the working tree has no changes (staged or unstaged)."""
    result = _run_git(["status", "--porcelain"], cwd=cwd)
    return result.success and not result.stdout.strip()


def _branch_exists(branch: str, cwd: Path | None = None) -> bool:
    """Check if a branch exists (local or remote-tracking)."""
    result = _run_git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=cwd,
    )
    return result.success


class GitBackend:
    """Git VCS backend — feature branch per task with squash merge on approval."""

    def __init__(self) -> None:
        self._base_branch: str | None = None  # the branch we started on (e.g. "main")
        self._base_commit: str | None = None  # the commit hash at start

    # ── Protocol implementation ──────────────────────────────────

    def is_available(self) -> bool:
        """Check if git CLI is available."""
        return _run_git(["version"]).success

    def init(self, cwd: Path | None = None) -> None:
        """Capture the current branch and commit as the starting base."""
        # Verify we're in a git repo
        repo_check = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
        if not repo_check.success:
            raise RuntimeError("Not a git repository (or git not available).")

        branch = _get_current_branch(cwd=cwd)
        if not branch:
            raise RuntimeError("Could not determine current git branch.")

        # Detached HEAD — not supported, user needs to be on a branch
        if branch == "HEAD":
            raise RuntimeError(
                "Git is in detached HEAD state. Please checkout a branch first "
                "(e.g. `git checkout main`)."
            )

        commit = _get_commit_hash("HEAD", cwd=cwd)
        if not commit:
            raise RuntimeError("Could not determine current HEAD commit.")

        # Verify working tree is clean — we don't want to lose uncommitted work
        if not _is_clean_working_tree(cwd=cwd):
            raise RuntimeError(
                "Working tree has uncommitted changes. Please commit or stash "
                "them before running tasker with --vcs git."
            )

        self._base_branch = branch
        self._base_commit = commit
        logger.info("Git init: base_branch=%s, base_commit=%s", branch, commit[:12])

    def begin_task(self, task: Task, cwd: Path | None = None) -> None:
        """Create and checkout a feature branch for the task."""
        if not self._base_branch:
            return

        branch_name = f"{_BRANCH_PREFIX}{_sanitize_branch_name(task.label)}"

        # Guard against reserved names
        if branch_name.replace(_BRANCH_PREFIX, "") in _RESERVED_BRANCHES:
            raise RuntimeError(
                f"Branch name {branch_name!r} conflicts with a reserved name."
            )

        # If branch already exists (e.g. from a previous interrupted run),
        # delete it so we start fresh from the base.
        if _branch_exists(branch_name, cwd=cwd):
            logger.warning(
                "Branch %s already exists — deleting and recreating.", branch_name
            )
            _run_git(["branch", "-D", branch_name], cwd=cwd)

        # Make sure we're on the base branch before creating the feature branch
        _run_git(["checkout", self._base_branch], cwd=cwd)

        # Create and checkout the feature branch
        result = _run_git(["checkout", "-b", branch_name], cwd=cwd)

        if result.success:
            task.task_ref = branch_name
            task.base_ref = self._base_commit
            logger.info("Created feature branch: %s", branch_name)
        else:
            raise RuntimeError(
                f"Failed to create branch {branch_name!r}: {result.stderr}"
            )

    def get_diff(self, task: Task, cwd: Path | None = None) -> str:
        """Return diff between base commit and the feature branch's working tree.

        This captures ALL changes on the feature branch, including uncommitted
        modifications and any commits the developer made.
        """
        if not task.base_ref:
            return ""

        # Diff against the base commit — includes staged, unstaged, and committed changes
        result = _run_git(["diff", task.base_ref], cwd=cwd)
        if result.success and result.stdout.strip():
            return result.stdout.strip()

        # Also check for staged changes (git diff alone won't show them against a ref)
        staged = _run_git(["diff", "--cached", task.base_ref], cwd=cwd)
        if staged.success and staged.stdout.strip():
            return staged.stdout.strip()

        return ""

    def commit_task(self, task: Task, cwd: Path | None = None) -> None:
        """Squash-merge the feature branch onto the base branch.

        Workflow:
        1. Capture any unstaged working-tree changes (e.g. task-file
           checkbox marks written by the orchestrator) into the feature
           branch so they survive the branch switch.
        2. Switch back to base branch
        3. ``git merge --squash <feature-branch>`` — stages all changes
        4. ``git commit -m <message>`` — single clean commit
        5. Delete the feature branch

        If the feature branch has no changes (empty diff), we skip the
        merge to avoid committing an empty commit.
        """
        if not self._base_branch or not task.task_ref:
            return

        feature_branch = task.task_ref

        # ── Step 0: Capture unstaged changes into the feature branch ──
        # The orchestrator may have written to the task file (e.g. marking
        # a checkbox [x]) *after* the developer finished but *before*
        # calling commit_task.  We must fold those changes into the feature
        # branch so they survive the upcoming ``git checkout`` of the base.
        status = _run_git(["status", "--porcelain"], cwd=cwd)
        if status.success and status.stdout.strip():
            # There are uncommitted changes — stage them
            _run_git(["add", "-A"], cwd=cwd)
            # If the developer made commits on this branch, amend the last
            # one.  Otherwise (HEAD == base_commit) a regular commit is
            # needed to avoid amending the base and creating divergent history.
            head = _get_commit_hash("HEAD", cwd=cwd)
            if head and head != self._base_commit:
                _run_git(["commit", "--amend", "--no-edit"], cwd=cwd)
            else:
                _run_git(
                    ["commit", "-m", task.vcs_description],
                    cwd=cwd,
                )

        # Check if there are any changes to merge
        diff_check = _run_git(
            ["diff", "--quiet", self._base_commit or "HEAD", feature_branch],
            cwd=cwd,
        )
        has_changes = (
            not diff_check.success
        )  # git diff --quiet returns 1 if there are diffs

        if not has_changes:
            logger.info(
                "No changes on feature branch %s — skipping merge.", feature_branch
            )
            # Still switch back to base and clean up
            _run_git(["checkout", self._base_branch], cwd=cwd)
            if _branch_exists(feature_branch, cwd=cwd):
                _run_git(["branch", "-D", feature_branch], cwd=cwd)
            return

        # Step 1: Switch back to base branch
        checkout = _run_git(["checkout", self._base_branch], cwd=cwd)
        if not checkout.success:
            raise RuntimeError(
                f"Failed to checkout {self._base_branch}: {checkout.stderr}"
            )

        # Step 2: Squash-merge the feature branch
        merge = _run_git(["merge", "--squash", feature_branch], cwd=cwd)
        if not merge.success:
            raise RuntimeError(
                f"Failed to squash-merge {feature_branch}: {merge.stderr}"
            )

        # Step 3: Commit with the task description
        commit = _run_git(
            ["commit", "-m", task.vcs_description],
            cwd=cwd,
        )
        if not commit.success:
            # Merge was staged but commit failed — unstage to leave repo in clean state
            _run_git(["reset", "--hard", "HEAD"], cwd=cwd)
            raise RuntimeError(f"Failed to commit squash-merge: {commit.stderr}")

        # Update base commit so the next task branches from here
        new_head = _get_commit_hash("HEAD", cwd=cwd)
        if new_head:
            self._base_commit = new_head

        # Step 4: Delete the feature branch
        if _branch_exists(feature_branch, cwd=cwd):
            _run_git(["branch", "-D", feature_branch], cwd=cwd)

        logger.info(
            "Squash-merged %s → %s: %s",
            feature_branch,
            self._base_branch,
            task.vcs_description[:60],
        )
