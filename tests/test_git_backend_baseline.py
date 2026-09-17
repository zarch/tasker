"""Dirty-tree startup creates a baseline commit instead of failing."""

import subprocess
from pathlib import Path

from tasker.vcs.git_backend import GitBackend


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_dirty_startup_creates_baseline(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("one")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    (tmp_path / "dirty.txt").write_text("pre-existing")  # uncommitted

    backend = GitBackend()
    backend.init(cwd=tmp_path)  # must not raise

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"], cwd=tmp_path, capture_output=True, text=True
    ).stdout
    assert "tasker baseline" in log
