"""Unit tests for tasker.watchdog — verdict engine with a fake /proc."""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path

import pytest

from tasker.watchdog import (
    BREAKER,
    DOWN,
    DONE,
    DUP,
    NOMANIFEST,
    OK,
    STALE,
    ProcInfo,
    evaluate,
    heartbeat_path,
    manifest_path,
    crashcount_path,
    mark_run_done,
    match_run_pids,
    watchdog_log_path,
    write_heartbeat,
    write_run_manifest,
)


class FakeProc:
    """Injectable process view: SIGKILL removes, everything else records."""

    def __init__(self, procs: list[ProcInfo]):
        self._procs = list(procs)
        self.signals: list[tuple[int, int]] = []

    def procs(self) -> list[ProcInfo]:
        return list(self._procs)

    def alive(self, pid: int) -> bool:
        return any(p.pid == pid for p in self._procs)

    def signal(self, pid: int, sig: int) -> None:
        self.signals.append((pid, sig))
        if sig == signal.SIGKILL:
            self._procs = [p for p in self._procs if p.pid != pid]


def _proc(
    pid: int, task_file: Path, subcmd: str = "main", starttime: int = 100, ppid: int = 1
) -> ProcInfo:
    return ProcInfo(
        pid=pid,
        argv=("/usr/bin/python", "/bin/tasker", subcmd, str(task_file), "--vcs", "jj"),
        starttime=starttime,
        ppid=ppid,
    )


@pytest.fixture()
def task_file(tmp_path: Path) -> Path:
    return tmp_path / "99_todo.md"


def _write_manifest(
    task_file: Path, *, pid: int = 42, done: bool = False, argv: list[str] | None = None
) -> dict:
    payload = {
        "argv": argv
        or ["/usr/bin/python", "/bin/tasker", "main", str(task_file), "--vcs", "jj"],
        "pid": pid,
        "pgid": pid,
        "started_at": "2026-10-07T06:00:00+00:00",
        "cwd": str(task_file.parent),
        "task_file": str(task_file),
        "ledger_path": str(task_file.parent / "99_todo.iterations.jsonl"),
        "done": done,
    }
    manifest_path(task_file).write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _touch_aged(path: Path, age_s: float, now: float | None = None) -> None:
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    stamp = now - age_s
    import os

    os.utime(path, (stamp, stamp))


class TestSupervisedSide:
    def test_manifest_roundtrip(self, task_file, monkeypatch):
        monkeypatch.setattr("sys.argv", ["tasker", "main", str(task_file)])
        write_run_manifest(task_file, ledger_path=task_file.parent / "ledger.jsonl")
        payload = json.loads(manifest_path(task_file).read_text())
        # argv[0] is resolved to an absolute path so replay never depends on PATH
        assert payload["argv"][0].endswith("/tasker")
        assert payload["argv"][1:] == ["main", str(task_file)]
        assert payload["done"] is False
        assert payload["ledger_path"].endswith("ledger.jsonl")
        assert payload["pid"] > 0

    def test_mark_run_done_and_missing_manifest_noop(self, task_file):
        mark_run_done(task_file)  # missing manifest: must not raise
        _write_manifest(task_file)
        mark_run_done(task_file)
        assert json.loads(manifest_path(task_file).read_text())["done"] is True

    def test_heartbeat_writes_task_label(self, task_file):
        write_heartbeat(task_file, "Port.T1")
        data = json.loads(heartbeat_path(task_file).read_text())
        assert data["task"] == "Port.T1"
        assert data["ts"] > 0


class TestMatching:
    def test_matches_main_plus_task_file(self, task_file):
        procs = [
            _proc(1, task_file),
            _proc(2, task_file, subcmd="watchdog"),
            _proc(3, task_file.parent / "other.md"),
            _proc(4, task_file, subcmd="main"),
        ]
        matched = match_run_pids(procs, task_file, exclude_pid=4)
        assert [p.pid for p in matched] == [1]

    def test_watchdog_argv_never_matches_itself(self, task_file):
        assert match_run_pids([_proc(1, task_file, subcmd="watchdog")], task_file) == []


class TestVerdicts:
    def test_nomanifest(self, task_file):
        v = evaluate(task_file, FakeProc([]), dry_run=True)
        assert v.name == NOMANIFEST

    def test_done(self, task_file):
        _write_manifest(task_file, done=True)
        v = evaluate(task_file, FakeProc([]), dry_run=True)
        assert v.name == DONE

    def test_ok_with_fresh_heartbeat(self, task_file):
        _write_manifest(task_file, pid=42)
        _touch_aged(heartbeat_path(task_file), 60)
        v = evaluate(task_file, FakeProc([_proc(42, task_file)]), dry_run=True)
        assert v.name == OK
        assert "pid=42" in v.detail

    def test_unmanaged_run_without_manifest_is_ok_not_stale(self, task_file):
        # Old-code run: alive, but writes no heartbeat/ledger signals — must
        # never be STALE-killed for signals that cannot exist.
        v = evaluate(task_file, FakeProc([_proc(42, task_file)]), dry_run=True)
        assert v.name == OK
        assert "unmanaged" in v.detail

    def test_stale_kills_run(self, task_file):
        _write_manifest(task_file, pid=42)
        _touch_aged(heartbeat_path(task_file), 99999)
        provider = FakeProc([_proc(42, task_file)])
        v = evaluate(task_file, provider, kill_grace=0)
        assert v.name == STALE
        assert 42 in v.killed
        assert (42, signal.SIGTERM) in provider.signals

    def test_stale_ignored_when_ledger_fresh(self, task_file):
        # The 2026-10-07 07:59 incident: frozen log, live ledger.
        m = _write_manifest(task_file, pid=42)
        _touch_aged(heartbeat_path(task_file), 99999)
        _touch_aged(Path(m["ledger_path"]), 120)
        v = evaluate(task_file, FakeProc([_proc(42, task_file)]), dry_run=True)
        assert v.name == OK

    def test_dup_keeps_oldest(self, task_file):
        _write_manifest(task_file)
        provider = FakeProc(
            [_proc(100, task_file, starttime=5), _proc(200, task_file, starttime=9)]
        )
        v = evaluate(task_file, provider, kill_grace=0)
        assert v.name == DUP
        assert v.killed == [200]
        assert provider.alive(100)

    def test_dup_dry_run_kills_nothing(self, task_file):
        _write_manifest(task_file)
        provider = FakeProc(
            [_proc(100, task_file, starttime=5), _proc(200, task_file, starttime=9)]
        )
        v = evaluate(task_file, provider, dry_run=True)
        assert v.name == DUP
        assert provider.signals == []

    def test_down_relaunches_from_manifest_argv(self, task_file):
        m = _write_manifest(task_file, pid=42)  # the old, dead run
        spawned: list[tuple[list[str], str]] = []

        def fake_spawn(argv, cwd, out_path):
            spawned.append((argv, cwd))
            return 777

        v = evaluate(task_file, FakeProc([]), spawn=fake_spawn, settle_secs=0.0)
        assert v.name == DOWN and v.relaunched
        assert spawned[0][0] == m["argv"]
        assert spawned[0][1] == m["cwd"]
        crashes = json.loads(crashcount_path(task_file).read_text())
        assert crashes["spawned_pid"] == 777
        assert crashes["count"] == 0  # previous run started properly → forgiven


class TestCircuitBreaker:
    def test_three_never_started_spawns_trip_breaker(self, task_file):
        _write_manifest(task_file, pid=42)
        verdicts = []
        for _ in range(3):
            verdicts.append(
                evaluate(
                    task_file,
                    FakeProc([]),
                    settle_secs=0.0,
                    max_crashes=3,
                    # spawn "succeeds" but the new run never rewrites the manifest
                    spawn=lambda argv, cwd, out: 777,
                )
            )
        assert [v.name for v in verdicts] == [DOWN, DOWN, DOWN]
        assert all(v.relaunched for v in verdicts)
        v4 = evaluate(
            task_file,
            FakeProc([]),
            settle_secs=0.0,
            max_crashes=3,
            spawn=lambda argv, cwd, out: 778,
        )
        assert v4.name == BREAKER
        assert not v4.relaunched

    def test_properly_started_run_resets_counter(self, task_file):
        _write_manifest(task_file, pid=42)
        evaluate(
            task_file, FakeProc([]), settle_secs=0.0, spawn=lambda argv, cwd, out: 777
        )
        # The spawned run 777 started properly (rewrote manifest), then died.
        _write_manifest(task_file, pid=777)
        v = evaluate(
            task_file, FakeProc([]), settle_secs=0.0, spawn=lambda argv, cwd, out: 888
        )
        assert v.name == DOWN and v.relaunched
        crashes = json.loads(crashcount_path(task_file).read_text())
        assert crashes["count"] == 0


class TestWatchdogLog:
    def test_verdict_appended_to_log(self, task_file):
        _write_manifest(task_file, pid=42)
        _touch_aged(heartbeat_path(task_file), 60)
        evaluate(task_file, FakeProc([_proc(42, task_file)]), dry_run=True)
        log_line = watchdog_log_path(task_file).read_text()
        assert " OK " in log_line and "pid=42" in log_line
