"""checkpoint_task() behavior for git and jj backends.

Checkpoints snapshot mid-task work so a crash never loses uncommitted
changes; they must be no-ops on a clean tree, and the final
commit_task must still produce one clean commit containing everything.
"""

import subprocess
from pathlib import Path

from tasker.models import Task
from tasker.vcs.git_backend import GitBackend


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _make_task() -> Task:
    return Task(phase_index=0, task_index=0, text="Do the thing")


def _init_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.txt").write_text("base")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")


def test_git_checkpoint_clean_tree_is_noop(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = GitBackend()
    backend.init(cwd=tmp_path)
    task = _make_task()
    backend.begin_task(task, cwd=tmp_path)

    assert backend.checkpoint_task(task, cwd=tmp_path) is False
    log = _git(tmp_path, "log", "--oneline")
    assert "checkpoint" not in log


def test_git_checkpoint_commits_and_squashes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    backend = GitBackend()
    backend.init(cwd=tmp_path)
    task = _make_task()
    backend.begin_task(task, cwd=tmp_path)

    (tmp_path / "a.txt").write_text("work A")
    assert backend.checkpoint_task(task, cwd=tmp_path) is True
    (tmp_path / "b.txt").write_text("work B")
    assert backend.checkpoint_task(task, cwd=tmp_path) is True

    # Final commit_task squashes checkpoints into one clean commit
    backend.commit_task(task, cwd=tmp_path)
    assert _git(tmp_path, "branch", "--show-current").strip() == "main"
    log = _git(tmp_path, "log", "--oneline")
    assert "checkpoint" not in log  # checkpoints collapsed
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").exists()
    # Diff on main contains exactly one task commit on top of init
    assert len(_git(tmp_path, "log", "--oneline").strip().splitlines()) == 2


def test_jj_checkpoint_lifecycle(tmp_path: Path) -> None:
    from tasker.vcs.jj_backend import JJBackend

    _git(tmp_path, "init", "-b", "main")  # colocated repo works with jj
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "base.txt").write_text("base")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    subprocess.run(
        ["jj", "git", "init", "--colocate"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    backend = JJBackend()
    backend.init(cwd=tmp_path)
    task = _make_task()
    backend.begin_task(task, cwd=tmp_path)
    base = task.base_ref

    # Clean working copy → no checkpoint
    assert backend.checkpoint_task(task, cwd=tmp_path) is False

    (tmp_path / "a.txt").write_text("work A")
    assert backend.checkpoint_task(task, cwd=tmp_path) is True
    # task_ref advanced to the fresh working change
    assert task.task_ref != base
    # Full diff since base still visible for QA
    assert "a.txt" in backend.get_diff(task, cwd=tmp_path)

    (tmp_path / "b.txt").write_text("work B")
    assert backend.checkpoint_task(task, cwd=tmp_path) is True

    backend.commit_task(task, cwd=tmp_path)
    # Everything landed; next task branches from the finalized chain
    files = subprocess.run(
        ["jj", "file", "list"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "a.txt" in files and "b.txt" in files
    next_task = Task(phase_index=0, task_index=1, text="Next")
    backend.begin_task(next_task, cwd=tmp_path)  # must not raise
