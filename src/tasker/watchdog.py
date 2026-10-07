"""tasker watchdog — supervise a ``tasker main`` run and recover it.

The supervised side is the orchestrator: at run start it writes a *run
manifest* (``<stem>.tasker-run.json`` — argv, pid, cwd, ledger path), at
every ``task.starting`` it touches a *heartbeat* file, and on
``all_tasks.complete`` it marks the manifest done.  All bookkeeping is
best-effort and can never break the run.

The supervising side is this module's :func:`evaluate` plus the ``tasker
watchdog`` CLI command (single shot, or looping with ``--interval``).
One invocation produces exactly one verdict:

===========  =======================================================
OK           exactly one run process alive — nothing to do
DOWN         no run process — relaunch by replaying the manifest argv
             (process group isolated, output appended to
             ``<stem>.tasker-relaunch.out``)
DUP          multiple run processes racing — keep the oldest, TERM/KILL
             the rest
STALE        process alive but every freshness signal (heartbeat, ledger)
             is older than ``--stale-secs`` — kill it; the relaunch happens
             on the next tick's DOWN verdict
DONE         manifest says the run finished the whole backlog
BREAKER      circuit breaker tripped (``--max-crashes`` consecutive
             relaunches that died before even starting) — no relaunch,
             exit code 1
NOMANIFEST   no manifest and no run process — nothing to supervise
===========  =======================================================

Kill semantics: TERM the pid and all its descendants, wait ``--kill-grace``
seconds, KILL the survivors.  Freshness uses the FRESHEST of the signals:
a live run can freeze its stdout (deleted inode) while the ledger keeps
moving, and vice versa.

The relaunch inherits the watchdog's own environment — run the watchdog
with whatever ``PATH``/env the run needs.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

# ---------------------------------------------------------------- supervised side


def _stem_path(task_file: Path, suffix: str) -> Path:
    return task_file.parent / f"{task_file.stem}{suffix}"


def manifest_path(task_file: Path) -> Path:
    return _stem_path(task_file, ".tasker-run.json")


def heartbeat_path(task_file: Path) -> Path:
    return _stem_path(task_file, ".tasker-heartbeat")


def crashcount_path(task_file: Path) -> Path:
    return _stem_path(task_file, ".tasker-crashcount.json")


def watchdog_log_path(task_file: Path) -> Path:
    return _stem_path(task_file, ".tasker-watchdog.log")


def relaunch_out_path(task_file: Path) -> Path:
    return _stem_path(task_file, ".tasker-relaunch.out")


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_run_manifest(
    task_file: Path, *, ledger_path: Path | str | None, cwd: Path | str | None = None
) -> None:
    """Called by the orchestrator at run() start — defines the supervised run."""
    argv = list(sys.argv)
    if argv and "/" not in argv[0] and not os.path.isabs(argv[0]):
        import shutil

        resolved = shutil.which(argv[0])
        if resolved:
            argv[0] = resolved
    _atomic_write_json(
        manifest_path(Path(task_file)),
        {
            "argv": argv,
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "cwd": str(Path(cwd) if cwd else Path.cwd()),
            "task_file": str(Path(task_file)),
            "ledger_path": str(ledger_path) if ledger_path else None,
            "done": False,
        },
    )


def mark_run_done(task_file: Path) -> None:
    """Called by the orchestrator when the whole backlog is complete."""
    path = manifest_path(Path(task_file))
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    payload["done"] = True
    _atomic_write_json(path, payload)


def write_heartbeat(task_file: Path, task_label: str) -> None:
    """Called by the orchestrator at every task.starting."""
    path = heartbeat_path(Path(task_file))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"ts": time.time(), "task": task_label}) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


# ---------------------------------------------------------------- process view


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    argv: tuple[str, ...]
    starttime: int  # /proc/<pid>/stat field 22 — immune to pid reuse
    ppid: int


class ProcProvider(Protocol):
    """Process view — injectable so tests never touch the real /proc."""

    def procs(self) -> list[ProcInfo]: ...

    def alive(self, pid: int) -> bool: ...

    def signal(self, pid: int, sig: int) -> None: ...


class LinuxProc:
    """Real /proc-backed provider (Linux only, by design)."""

    def procs(self) -> list[ProcInfo]:
        out: list[ProcInfo] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
                argv = tuple(
                    p.decode("utf-8", "replace") for p in raw.split(b"\0") if p
                )
                stat = (entry / "stat").read_text(encoding="utf-8")
                # comm may contain spaces/parens — parse after the last ')'
                rest = stat[stat.rfind(")") + 2 :].split()
                out.append(
                    ProcInfo(
                        pid=int(entry.name),
                        argv=argv,
                        starttime=int(rest[19]),
                        ppid=int(rest[1]),
                    )
                )
            except (OSError, ValueError, IndexError):
                continue
        return out

    def alive(self, pid: int) -> bool:
        return Path(f"/proc/{pid}").exists()

    def signal(self, pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def match_run_pids(
    procs: list[ProcInfo], task_file: Path, exclude_pid: int | None = None
) -> list[ProcInfo]:
    """A supervised run = argv containing the literal subcommand 'main' and
    the exact task-file path.  The watchdog's own argv has 'watchdog', so it
    never matches itself."""
    tf = str(Path(task_file))
    return [
        p for p in procs if p.pid != exclude_pid and "main" in p.argv and tf in p.argv
    ]


# ---------------------------------------------------------------- verdict engine


OK = "OK"
DOWN = "DOWN"
DUP = "DUP"
STALE = "STALE"
DONE = "DONE"
BREAKER = "BREAKER"
NOMANIFEST = "NOMANIFEST"

EXIT_BREAKER = 1


@dataclass
class Verdict:
    name: str
    detail: str
    killed: list[int] = field(default_factory=list)
    relaunched: bool = False


def _file_age(path: Path | None, now: float) -> float:
    """Seconds since mtime; missing file counts as infinitely old."""
    if path is None or not path.is_file():
        return float("inf")
    return max(0.0, now - path.stat().st_mtime)


def freshness_ages(
    task_file: Path, manifest: dict | None, now: float
) -> dict[str, float]:
    signals: dict[str, Path | None] = {
        "heartbeat": heartbeat_path(Path(task_file)),
        "ledger": (
            Path(manifest["ledger_path"])
            if manifest and manifest.get("ledger_path")
            else None
        ),
    }
    return {name: _file_age(path, now) for name, path in signals.items()}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _descendants(procs: list[ProcInfo], roots: list[int]) -> set[int]:
    """Transitive children of roots (excluding the roots themselves)."""
    kids: set[int] = set()
    frontier = set(roots)
    while frontier:
        nxt = {p.pid for p in procs if p.ppid in frontier and p.pid not in kids}
        kids |= nxt
        frontier = nxt
    return kids


def kill_tree(provider: ProcProvider, pids: list[int], grace: float) -> list[int]:
    """TERM pid + descendants, wait grace, KILL survivors.  Returns killed pids."""
    procs = provider.procs()
    targets = list(pids) + sorted(_descendants(procs, pids))
    for pid in targets:
        provider.signal(pid, signal.SIGTERM)
    if grace > 0:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and any(provider.alive(p) for p in targets):
            time.sleep(0.2)
    killed: list[int] = []
    for pid in targets:
        if provider.alive(pid):
            provider.signal(pid, signal.SIGKILL)
        killed.append(pid)
    return killed


def _spawn_process(argv: list[str], cwd: str, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as out:
        proc = subprocess.Popen(  # noqa: S603 - argv comes from our own manifest
            argv,
            cwd=cwd,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc.pid


SpawnFn = Callable[[list[str], str, Path], int]


def evaluate(
    task_file: Path,
    provider: ProcProvider,
    *,
    now: float | None = None,
    stale_secs: float = 4000.0,
    max_crashes: int = 3,
    kill_grace: float = 20.0,
    settle_secs: float = 2.0,
    dry_run: bool = False,
    exclude_pid: int | None = None,
    spawn: SpawnFn | None = None,
) -> Verdict:
    """Pure-ish verdict engine: decides and acts (kill/relaunch) unless dry_run."""
    now = time.time() if now is None else now
    tf = Path(task_file)
    spawn = spawn or _spawn_process
    mpath = manifest_path(tf)
    manifest = _read_json(mpath)
    crashes = _read_json(crashcount_path(tf)) or {}
    matching = match_run_pids(provider.procs(), tf, exclude_pid=exclude_pid)

    def log(verdict: Verdict) -> Verdict:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        line = f"{stamp} {verdict.name} {verdict.detail}\n"
        try:
            with watchdog_log_path(tf).open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
        return verdict

    # --- DONE beats everything: the run finished on its own.
    if manifest and manifest.get("done") and not matching:
        return log(Verdict(DONE, "backlog complete per manifest"))

    if len(matching) > 1:  # --- DUP: keep the oldest (smallest starttime)
        matching.sort(key=lambda p: p.starttime)
        keep, rest = matching[0], [p.pid for p in matching[1:]]
        if dry_run:
            return log(Verdict(DUP, f"would kill {rest}, keep {keep.pid}"))
        killed = kill_tree(provider, rest, kill_grace)
        return log(Verdict(DUP, f"kept {keep.pid}, killed {killed}", killed=killed))

    if len(matching) == 1:  # --- alive: OK, or STALE if every signal froze
        run = matching[0]
        if manifest is None:
            # Old-code run (or someone else's): alive but writing no signals —
            # supervise-less OK; never kill what we cannot measure.
            return log(Verdict(OK, f"pid={run.pid} unmanaged (no manifest)"))
        ages = freshness_ages(tf, manifest, now)
        freshest = min(ages.values())
        if freshest > stale_secs:
            age_txt = "/".join(f"{k} {_fmt_age(v)}" for k, v in ages.items())
            if dry_run:
                return log(Verdict(STALE, f"would kill {run.pid} (signals {age_txt})"))
            killed = kill_tree(provider, [run.pid], kill_grace)
            return log(
                Verdict(STALE, f"killed {killed} (signals {age_txt})", killed=killed)
            )
        age_txt = " ".join(f"{k}={_fmt_age(v)}" for k, v in ages.items())
        return log(Verdict(OK, f"pid={run.pid} {age_txt}"))

    # --- no run process: DOWN / DONE-without-manifest / BREAKER / NOMANIFEST
    if manifest is None:
        return log(Verdict(NOMANIFEST, "no manifest, no run — nothing to supervise"))

    if dry_run:
        return log(Verdict(DOWN, "would relaunch: " + " ".join(manifest["argv"])))

    # Circuit breaker: a spawn that never rewrote the manifest (its pid is
    # absent from manifest.pid) died before reaching run() — an instant
    # crash (bad flag, import error).  A run that started properly and died
    # mid-flight is ordinary recovery, not crash-loop evidence.
    if _never_started(crashes, manifest):
        crashes["count"] = int(crashes.get("count", 0)) + 1
    else:
        crashes["count"] = 0
    if crashes["count"] >= max_crashes:
        _atomic_write_json(crashcount_path(tf), crashes)
        return log(
            Verdict(
                BREAKER,
                f"{crashes['count']} relaunches died before starting — "
                f"giving up (delete {crashcount_path(tf).name} to reset)",
            )
        )

    pid = spawn(
        list(manifest["argv"]), str(manifest.get("cwd") or "."), relaunch_out_path(tf)
    )
    crashes["last_relaunch_ts"] = now
    crashes["spawned_pid"] = pid
    _atomic_write_json(crashcount_path(tf), crashes)
    if settle_secs > 0:  # TOCTOU: someone else may have started one too
        time.sleep(settle_secs)
        others = [
            p
            for p in match_run_pids(provider.procs(), tf, exclude_pid=pid)
            if p.pid != pid
        ]
        if others:
            kill_tree(provider, [pid], kill_grace)
            return log(
                Verdict(
                    DOWN, f"lost race to pid {others[0].pid}, killed own spawn {pid}"
                )
            )
    return log(Verdict(DOWN, f"relaunched pid={pid}", relaunched=True))


def _fmt_age(seconds: float) -> str:
    return "none" if seconds == float("inf") else f"{int(seconds)}s"


def _never_started(crashes: dict, manifest: dict) -> bool:
    """True when the pid we spawned is absent from the manifest — it died
    before orchestrator.run() could rewrite the manifest."""
    spawned = crashes.get("spawned_pid")
    return bool(spawned) and manifest.get("pid") != spawned


def run_watchdog(task_file: Path, *, interval: float = 0.0, **kwargs) -> int:
    """Entry used by the CLI: single shot (interval=0) or loop until DONE/BREAKER."""
    tf = Path(task_file)
    while True:
        verdict = evaluate(tf, LinuxProc(), **kwargs)
        print(f"{verdict.name}: {verdict.detail}")
        if verdict.name in (DONE, NOMANIFEST):
            return 0
        if verdict.name == BREAKER:
            return EXIT_BREAKER
        if interval <= 0:
            return 0
        time.sleep(interval)
