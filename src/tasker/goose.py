"""Goose subprocess runner — wraps `goose run` with JSON output parsing."""

from __future__ import annotations

import errno
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from .models import RateLimitConfig

log = structlog.get_logger(__name__)


# -- Patterns that indicate a transient connection / rate-limit error -------

_CONNECTION_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"error:\s*not connected",
        r"connection\s+(refused|reset|timed?\s*out|closed|dropped)",
        r"rate\s*limit",
        r"too\s+many\s+requests",
        r"429",
        r"503\s+service\s+unavailable",
        r"502\s+bad\s+gateway",
        r"quota\s+(exceeded|limit)",
        r"temporarily\s+unavailable",
        r"epoll\s+wait",
        r"broken\s+pipe",
    )
]


def is_connection_error(stderr: str, return_code: int | None = None) -> bool:
    """Return True if the failure looks like a transient connection/rate-limit issue.

    Heuristic: the goose subprocess exited with a non-zero code (typically 1)
    and its stderr contains a known transient-error phrase.
    """
    if not stderr:
        return False
    # Fast path: check against known patterns
    for pat in _CONNECTION_ERROR_PATTERNS:
        if pat.search(stderr):
            return True
    return False


def is_silent_crash(result: GooseRunResult) -> bool:
    """Return True if goose crashed with no output at all.

    This happens when the LLM provider rate-limits or refuses the request
    but goose exits without printing anything to stderr.  These are
    transient errors that should be retried with backoff, not burned
    through the recovery-stage budget.

    The heuristic: rc!=0 AND empty stdout AND empty stderr AND not a
    timeout (timeouts have their own handling path).
    """
    return (
        not result.success
        and not result.timed_out
        and result.return_code != 0
        and not result.raw_stdout.strip()
        and not result.raw_stderr.strip()
    )


@dataclass
class GooseRunResult:
    """Result of a goose run invocation."""

    success: bool
    raw_stdout: str
    raw_stderr: str
    return_code: int
    parsed_json: dict | None = None
    duration_secs: float = 0.0
    timed_out: bool = False
    raw_envelope: str = ""  # original goose JSON output before extraction
    empty_output: bool = False  # True when goose returned rc=0 but no assistant text
    json_blocks_found: int = 0  # number of valid JSON blocks found in cascade
    json_blocks_cascade: bool = (
        False  # True when a malformed candidate appears after last valid block
    )
    assistant_turns: int = 0  # count of messages with role="assistant"
    total_turns: int = 0  # total message count in the envelope
    output_chars: int = 0  # len of assistant_text


def _extract_json_blocks(text: str) -> list[dict]:
    """Extract ALL valid JSON dicts from *text* in document order.

    Scans for two kinds of candidates:

    1. Fenced ````json ... ```` code blocks (via :func:`re.finditer`).
    2. Bare ``{ … }`` brace pairs.

    Each candidate is tried with :func:`json.loads`.  Successfully parsed
    dicts are collected and **deduplicated**: when two candidates parse to
    the same dict at overlapping character positions, only the earlier one
    is kept.

    Returns a list of dicts ordered by the first character position of the
    match in *text*.
    """
    # Each candidate: (start_pos, end_pos, parsed_dict)
    candidates: list[tuple[int, int, dict]] = []

    # 1) Fenced ```json ... ``` blocks
    fenced_re = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    for m in fenced_re.finditer(text):
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
            if isinstance(obj, dict):
                candidates.append((m.start(), m.end(), obj))
        except (json.JSONDecodeError, ValueError):
            pass

    # 2) Bare { … } brace pairs
    depth = 0
    brace_start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                brace_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and brace_start >= 0:
                try:
                    obj = json.loads(text[brace_start : i + 1])
                    if isinstance(obj, dict):
                        candidates.append((brace_start, i + 1, obj))
                except (json.JSONDecodeError, ValueError):
                    pass
                brace_start = -1

    # Sort by start position (stable — earlier matches first)
    candidates.sort(key=lambda t: t[0])

    # Deduplicate: when two candidates parse to equal dicts at overlapping
    # positions, keep only the earlier one.
    seen: list[tuple[int, int, dict]] = []
    for start, end, obj in candidates:
        dominated = False
        for s, e, prev in seen:
            # Overlap test: two ranges [s, e) and [start, end) overlap
            # iff s < end and start < e.
            if s < end and start < e and prev == obj:
                dominated = True
                break
        if not dominated:
            seen.append((start, end, obj))

    return [obj for _, _, obj in seen]


def _find_last_block_end(text: str, obj: dict) -> int:
    """Return the end position (exclusive) of the last brace-pair match of *obj* in *text*.

    Scans all ``{ … }`` regions and returns the end index of the last one
    that parses to the same dict as *obj*.  Returns -1 if not found.
    """
    last_end = -1
    depth = 0
    brace_start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                brace_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and brace_start >= 0:
                try:
                    parsed = json.loads(text[brace_start : i + 1])
                    if isinstance(parsed, dict) and parsed == obj:
                        last_end = i + 1
                except (json.JSONDecodeError, ValueError):
                    pass
                brace_start = -1
    return last_end


def _has_trailing_malformed(text: str, last_valid_end: int) -> bool:
    """Return True if a malformed JSON candidate appears after position *last_valid_end*.

    A malformed candidate is either:
    - A fenced ````json ... ```` block whose body is not valid JSON (or not a dict).
    - A bare ``{ … }`` brace pair whose content is not valid JSON (or not a dict).

    We only check candidates that start at or after *last_valid_end*.
    """
    # Check fenced blocks after the last valid block
    fenced_re = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
    for m in fenced_re.finditer(text):
        if m.start() < last_valid_end:
            continue
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
            if not isinstance(obj, dict):
                return True
        except (json.JSONDecodeError, ValueError):
            return True

    # Check bare brace pairs after the last valid block
    depth = 0
    brace_start = -1
    for i, ch in enumerate(text):
        if i < last_valid_end:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth = max(depth - 1, 0)
            continue
        if ch == "{":
            if depth == 0:
                brace_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and brace_start >= 0:
                try:
                    obj = json.loads(text[brace_start : i + 1])
                    if not isinstance(obj, dict):
                        return True
                except (json.JSONDecodeError, ValueError):
                    return True
                brace_start = -1

    return False


def _extract_last_assistant_text(raw_stdout: str) -> tuple[str, bool, int, int]:
    """Extract the last assistant message text from goose JSON output.

    goose run --output-format json returns: {"messages": [...]}
    Each message has {"role": "user|assistant", "content": [{"type": "text", "text": "..."}]}
    We concatenate assistant texts and return them for JSON extraction.

    Returns ``(text, empty_flag, assistant_turns, total_turns)`` where:

    - *text* is the concatenated assistant message content (or raw stdout
      when the envelope is not valid JSON).
    - *empty_flag* is True when there are no usable assistant messages —
      either the envelope has zero assistant messages at all, or ALL
      assistant messages are stale (none appear after the last user
      message, meaning goose replayed old history but produced no new
      response).
    - *assistant_turns* is the count of messages with role ``"assistant"``.
    - *total_turns* is ``len(messages)``.

    Stale-output guard: when the envelope has assistant messages but NONE
    appear after the last user message, the function returns
    ``("", True, 0, total)`` so stale JSON from a previous call cannot
    leak into the current iteration.
    """
    try:
        envelope = json.loads(raw_stdout)
        if isinstance(envelope, dict) and "messages" in envelope:
            messages = envelope["messages"]
            total_turns = len(messages)

            # Count assistant messages
            assistant_turns = sum(
                1 for msg in messages if msg.get("role") == "assistant"
            )

            # Find index of the last user message
            last_user_idx = -1
            for i, msg in enumerate(messages):
                if msg.get("role") == "user":
                    last_user_idx = i

            if last_user_idx == -1:
                # No user message at all — all assistant messages are stale
                # replayed history with no new response.
                if assistant_turns > 0:
                    return "", True, 0, total_turns
                # No user messages and no assistant messages
                return "", True, 0, total_turns

            # Collect only assistant messages that appear after the last
            # user message — these are the *new* responses from this call.
            new_assistant_texts: list[str] = []
            for msg in messages[last_user_idx + 1 :]:
                if msg.get("role") == "assistant":
                    for content in msg.get("content", []):
                        if content.get("type") == "text" and content.get("text"):
                            new_assistant_texts.append(content["text"])

            if new_assistant_texts:
                return (
                    "\n\n".join(new_assistant_texts),
                    False,
                    assistant_turns,
                    total_turns,
                )

            # No new assistant messages after last user message.
            # If there are assistant messages at all, this is stale output.
            if assistant_turns > 0:
                return "", True, 0, total_turns

            # Valid envelope but zero assistant messages — structural failure
            return "", True, 0, total_turns
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return raw_stdout, False, 0, 0


def build_goose_command(
    recipe_path: str | Path,
    session_name: str,
    params: dict[str, str] | None = None,
    max_turns: int = 80,
    model: str | None = None,
    provider: str | None = None,
) -> list[str]:
    """Build the goose CLI command list.

    Uses `--name` for session persistence (auto-resumes existing sessions).
    Uses `--params KEY=VALUE` to pass data into recipe templates.
    `--text` and `--recipe` are mutually exclusive, so we use --params.
    """
    cmd = [
        "goose",
        "run",
        "--with-builtin",
        "developer",
        "--recipe",
        str(recipe_path),
        "--name",
        session_name,
        "--output-format",
        "json",
        "--quiet",
        "--max-turns",
        str(max_turns),
    ]
    if params:
        for key, value in params.items():
            # Escape newlines and other shell-unsafe characters in values
            safe_value = (
                value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            )
            cmd.extend(["--params", f"{key}={safe_value}"])
    if model:
        cmd.extend(["--model", model])
    if provider:
        cmd.extend(["--provider", provider])
    return cmd


def _heartbeat_logger(
    session: str,
    stop_event: threading.Event,
    interval: float = 30.0,
) -> None:
    """Background thread that emits periodic heartbeat log events.

    Runs until ``stop_event`` is set.  Emits one ``goose.heartbeat`` event
    every *interval* seconds so the monitor log shows the orchestrator is
    still alive while waiting for a subprocess.
    """
    while not stop_event.wait(timeout=interval):
        log.debug(
            "goose.heartbeat",
            session=session,
            waiting_secs=interval,
        )


def _detect_cgroup_memory_limit() -> tuple[str, str] | None:
    """Detect sensible cgroup memory limits for the goose subprocess.

    Returns ``(memory_max, swap_max)`` as human-readable strings
    (e.g. ``("8G", "4G")``), or None when cgroup limiting is
    unavailable.

    Strategy — budget based on **MemAvailable** (what the kernel
    reports as actually free), not MemTotal:

    1. Read MemAvailable and SwapFree from /proc/meminfo.
    2. Goose RAM budget = 50% of MemAvailable (rounded to GB).
    3. Goose swap budget = 30% of SwapFree (rounded to GB).
    4. Reserve the rest for OS + desktop + tasker + spikes.

    On a 31 GB machine with 22 GB available and 12 GB swap free:
    MemoryMax=11G, MemorySwapMax=4G.  Total budget = 15G.
    This leaves ~11 GB RAM + 8 GB swap for the rest of the system.
    """
    try:
        with open("/proc/meminfo") as f:
            info: dict[str, int] = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])

        avail_kb = info.get("MemAvailable", 0)
        swap_free_kb = info.get("SwapFree", 0)
        if avail_kb == 0:
            return None

        avail_gb = avail_kb / (1024 * 1024)
        swap_free_gb = swap_free_kb / (1024 * 1024)

        # Goose RAM budget: 50% of what's currently available
        ram_gb = max(round(avail_gb * 0.50), 4)
        # Goose swap budget: 30% of free swap
        swap_gb = max(round(swap_free_gb * 0.30), 2)

        ram_str = f"{ram_gb}G"
        swap_str = f"{swap_gb}G"
        log.debug(
            "goose.cgroup_limit",
            avail_gb=round(avail_gb, 1),
            swap_free_gb=round(swap_free_gb, 1),
            ram_max=ram_str,
            swap_max=swap_str,
        )
        return (ram_str, swap_str)
    except (OSError, ValueError):
        return None


_systemd_run_available: bool | None = None


def _can_use_systemd_run() -> bool:
    """Check whether ``systemd-run --user --scope`` is available.

    The check is performed once and cached for the lifetime of the
    process so we don't spawn a subprocess on every goose invocation.
    """
    global _systemd_run_available
    if _systemd_run_available is not None:
        return _systemd_run_available

    import shutil

    if shutil.which("systemd-run") is None:
        _systemd_run_available = False
        return False
    # Quick smoke test — run a no-op command under a user scope.
    try:
        result = subprocess.run(
            ["systemd-run", "--user", "--scope", "true"],
            capture_output=True,
            timeout=5,
        )
        _systemd_run_available = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        _systemd_run_available = False
    return _systemd_run_available


def run_goose(
    recipe_path: str | Path,
    session_name: str,
    params: dict[str, str] | None = None,
    max_turns: int = 80,
    timeout_secs: int = 600,
    model: str | None = None,
    provider: str | None = None,
    cwd: str | Path | None = None,
    memory_limit: str | None = None,
) -> GooseRunResult:
    """Run goose synchronously and return parsed result.

    Uses Popen + communicate(timeout) so we can explicitly kill the
    goose process (and its children) when the timeout expires, rather
    than relying on subprocess.run which may leave orphans.

    A background heartbeat thread emits periodic ``goose.heartbeat``
    log events so the monitor log shows liveness during the wait.

    Returns a GooseRunResult with timed_out=True when the process is killed.

    When *memory_limit* is set (e.g. ``"12G"``), the goose subprocess is
    launched inside a ``systemd-run --user --scope`` with
    ``--property=MemoryMax=…`` so the kernel OOM killer targets the goose
    cgroup first, sparing the rest of the system.  When *memory_limit*
    is ``None`` (the default), a limit is auto-detected from
    ``/proc/meminfo`` if cgroup v2 + systemd --user are available.
    Set to ``""`` (empty string) to disable the cgroup limit entirely.
    """
    cmd = build_goose_command(
        recipe_path=recipe_path,
        session_name=session_name,
        params=params,
        max_turns=max_turns,
        model=model,
        provider=provider,
    )

    # ── cgroup memory isolation ────────────────────────────────────────
    # Wrap the goose command in systemd-run --user --scope with
    # MemoryMax + MemorySwapMax properties.  This creates a cgroup v2
    # leaf for the goose process tree.  If the cgroup exceeds either
    # limit, the kernel will invoke the OOM killer *only* inside that
    # cgroup — the desktop, tasker, and other user applications are
    # spared.
    #
    # --quiet suppresses "Running as unit: …" on stderr so that the
    # only stderr output comes from goose itself (making error
    # diagnosis possible).
    cgroup_limits: tuple[str, str] | None = None
    if memory_limit == "":
        # Explicitly disabled by caller
        cgroup_limits = None
    elif memory_limit:
        # Caller specified a RAM limit; derive a swap limit (50% of RAM)
        cgroup_limits = (memory_limit, f"{int(memory_limit.rstrip('GgMmKk')) // 2}G")
    else:
        # Auto-detect
        if _can_use_systemd_run():
            cgroup_limits = _detect_cgroup_memory_limit()

    if cgroup_limits:
        ram_max, swap_max = cgroup_limits
        cmd = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--property=MemoryMax={ram_max}",
            f"--property=MemorySwapMax={swap_max}",
            "--",
        ] + cmd
        log.info(
            "goose.cgroup_wrapped",
            session=session_name,
            memory_limit=ram_max,
            swap_limit=swap_max,
        )

    log.debug(
        "goose.launching",
        session=session_name,
        recipe=str(recipe_path),
        max_turns=max_turns,
        timeout_secs=timeout_secs,
        model=model,
        provider=provider,
        cwd=str(cwd) if cwd else None,
        param_keys=list(params.keys()) if params else [],
    )

    # Merge required env vars with the current process environment
    env = os.environ.copy()
    env["GOOSE_CONTEXT_STRATEGY"] = "summarize"
    env["GOOSE_AUTO_COMPACT_THRESHOLD"] = "0.55"

    # Cap cargo build parallelism to 1 to minimize peak memory.
    # Each linker (lld) uses 1-1.5 GB RSS, and each rustc thread uses
    # 0.5-1 GB.  With CARGO_BUILD_JOBS=1 the peak is ~3-4 GB for
    # compilation + 1 GB for goose = ~5 GB total, well within the
    # cgroup budget.  Higher values caused OOM kills even inside the
    # cgroup on machines with ≤32 GB RAM and heavy workspaces
    # (1277 transitive crates, Polars, Arrow, wgpu, geo).
    env.setdefault("CARGO_BUILD_JOBS", "1")

    # Make rustdoc use mold for doctest linking.
    # Rustdoc compiles each doctest as a separate binary, linking ALL crate
    # dependencies.  By default it uses /usr/bin/ld (~600 MB per process).
    # With mold, each linker uses ~300 MB — critical when 10-16 doctests
    # compile in parallel (3-5 GB vs 6-10 GB peak).
    # NOTE: rustflags in .cargo/config.toml are NOT propagated to rustdoc's
    # doctest compilation — only RUSTDOCFLAGS works here.
    env.setdefault("RUSTDOCFLAGS", "-C link-arg=-fuse-ld=mold")

    # Limit test runner parallelism.  Even with mold, running many doctest
    # or unit-test binaries concurrently can exhaust memory.  Serial
    # execution (1 thread) is safest on memory-constrained machines.
    env.setdefault("RUST_TEST_THREADS", "1")

    # Disable incremental compilation so sccache can cache artifacts.
    # Without this, sccache marks incremental builds as non-cacheable and
    # every cargo build/test recompiles from scratch (30+ seconds even with
    # no source changes). With CARGO_INCREMENTAL=0, sccache achieves 100%
    # cache hit rate on repeat builds.
    env.setdefault("CARGO_INCREMENTAL", "0")

    # Inject tasker src directory into PYTHONPATH so the goose agent
    # subprocess can import tasker modules (e.g. from tasker.schema import DevResponse).
    tasker_src = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = tasker_src + os.pathsep + env.get("PYTHONPATH", "")
    log.debug(
        "goose.pythonpath_injected", tasker_src=tasker_src, pythonpath=env["PYTHONPATH"]
    )

    start = time.monotonic()

    # Start heartbeat thread for monitor-log liveness
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_logger,
        args=(session_name, heartbeat_stop),
        daemon=True,
    )
    heartbeat_thread.start()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd) if cwd else None,
            # Start a new process group so we can kill the whole tree
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_secs)
            duration = time.monotonic() - start
            stdout = stdout.strip()
            stderr = stderr.strip()

            # Extract assistant text from the goose JSON envelope,
            # then try to parse a structured JSON response from it.
            assistant_text, empty_output, assistant_turns, total_turns = (
                _extract_last_assistant_text(stdout)
            )

            # Cascade: extract ALL valid JSON blocks; last valid block wins.
            blocks = _extract_json_blocks(assistant_text)
            json_blocks_found = len(blocks)
            parsed = blocks[-1] if blocks else None

            # Detect cascade: is there a malformed candidate after the last
            # valid block?
            json_blocks_cascade = False
            if blocks:
                # Find end position of the last valid block in the text
                last_obj = blocks[-1]
                # Scan for the last occurrence of this dict in brace pairs
                last_valid_end = _find_last_block_end(assistant_text, last_obj)
                if last_valid_end >= 0:
                    json_blocks_cascade = _has_trailing_malformed(
                        assistant_text, last_valid_end
                    )
            elif assistant_text:
                # No valid blocks found but there IS text — check if any
                # malformed candidate exists at all
                json_blocks_cascade = _has_trailing_malformed(assistant_text, 0)

            # Structural failure: goose exited cleanly (rc=0) but produced
            # zero assistant text — it never reached the LLM or silently
            # errored inside the JSON envelope.  Mark as not-successful so
            # the orchestrator's recovery pipeline handles it properly
            # instead of entering the endless malformed_output cycle.
            success = proc.returncode == 0 and not empty_output

            log.info(
                "goose.completed",
                session=session_name,
                return_code=proc.returncode,
                duration_secs=round(duration, 2),
                parsed=bool(parsed),
                stdout_len=len(assistant_text),
                stderr_len=len(stderr),
                empty_output=empty_output,
                json_blocks_found=json_blocks_found,
                json_blocks_cascade=json_blocks_cascade,
                assistant_turns=assistant_turns,
                total_turns=total_turns,
            )

            return GooseRunResult(
                success=success,
                raw_stdout=assistant_text,
                raw_stderr=stderr,
                return_code=proc.returncode,
                parsed_json=parsed,
                duration_secs=round(duration, 2),
                timed_out=False,
                raw_envelope=stdout[:5000] if stdout else "",
                empty_output=empty_output,
                json_blocks_found=json_blocks_found,
                json_blocks_cascade=json_blocks_cascade,
                assistant_turns=assistant_turns,
                total_turns=total_turns,
                output_chars=len(assistant_text),
            )
        except subprocess.TimeoutExpired:
            # Kill the entire process group (goose + any child processes)
            duration = time.monotonic() - start
            log.warning(
                "goose.timeout",
                session=session_name,
                timeout_secs=timeout_secs,
                duration_secs=round(duration, 2),
            )
            try:
                os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
            except (ProcessLookupError, OSError):
                proc.kill()
            proc.communicate()  # reap to avoid zombies
            timeout_minutes = timeout_secs / 60
            return GooseRunResult(
                success=False,
                raw_stdout="",
                raw_stderr=(
                    f"TIMEOUT after {timeout_secs}s ({timeout_minutes:.0f}min) \u2014 "
                    f"goose process killed"
                ),
                return_code=-1,
                duration_secs=round(duration, 2),
                timed_out=True,
            )
    except OSError as exc:
        duration = time.monotonic() - start
        if exc.errno == errno.E2BIG:
            # E2BIG: total argv+envp exceeds OS limits, or a single argument
            # exceeds MAX_ARG_STRLEN (128 KB on Linux).  This is typically
            # caused by a very large --params value (e.g. project_context with
            # a huge VCS diff).  Log a clear, actionable message so the caller
            # can truncate the diff or switch to file-based param passing.
            total_argv = sum(len(a) for a in cmd)
            log.error(
                "goose.argv_too_large",
                session=session_name,
                error=str(exc),
                argv_bytes=total_argv,
                hint="VCS diff or other param too large for execve(); "
                "truncate the diff or write it to a temp file",
            )
            return GooseRunResult(
                success=False,
                raw_stdout="",
                raw_stderr=(
                    f"Cannot start goose: argument list too long "
                    f"(argv ≈ {total_argv / 1024:.0f} KB). "
                    f"The VCS diff or another parameter exceeds the OS "
                    f"per-argument limit (~128 KB). "
                    f"Truncate the diff or use file-based parameter passing."
                ),
                return_code=-1,
                duration_secs=round(duration, 2),
                timed_out=False,
            )
        log.error("goose.launch_failed", session=session_name, error=str(exc))
        return GooseRunResult(
            success=False,
            raw_stdout="",
            raw_stderr=f"Failed to start goose process: {exc}",
            return_code=-1,
            duration_secs=round(duration, 2),
            timed_out=False,
        )
    except Exception as exc:
        duration = time.monotonic() - start
        log.error("goose.launch_failed", session=session_name, error=str(exc))
        return GooseRunResult(
            success=False,
            raw_stdout="",
            raw_stderr=f"Failed to start goose process: {exc}",
            return_code=-1,
            duration_secs=round(duration, 2),
            timed_out=False,
        )
    finally:
        # Always stop the heartbeat thread
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)


def run_goose_with_backoff(
    recipe_path: str | Path,
    session_name: str,
    params: dict[str, str] | None = None,
    max_turns: int = 80,
    timeout_secs: int = 600,
    model: str | None = None,
    provider: str | None = None,
    cwd: str | Path | None = None,
    rate_limit: RateLimitConfig | None = None,
    memory_limit: str | None = None,
    fallback_model: str | None = None,
    fallback_provider: str | None = None,
) -> GooseRunResult:
    """Run goose with automatic retry on transient connection errors.

    Wraps :func:`run_goose` and adds three layers of resilience:

    1. **Silent-crash detection**: when goose exits rc!=0 with completely
       empty output (no stdout, no stderr), this is treated as a transient
       provider failure (rate-limit, temporary outage) and retried with
       exponential backoff.

    2. **Connection-error backoff**: when stderr contains a known transient
       pattern ("429", "rate limit", "not connected", etc.), retry with
       exponential backoff up to *max_retries*.

    3. **Fallback model**: after the primary model's retries are exhausted,
       try the fallback model/provider (if configured).  This lets the
       orchestrator keep making progress — e.g. dev falls back to a local
       Ollama model while QA falls back to a different cloud provider.
       The fallback is per-call only; the next orchestrator turn reuses
       the primary model.

    *memory_limit* is forwarded to :func:`run_goose`.  See its docstring
    for details.

    Returns the last :class:`GooseRunResult` -- either a successful run or
    the final failure after exhausting all retries and fallbacks.
    """
    if rate_limit is None:
        rate_limit = RateLimitConfig()
    if not rate_limit.enabled:
        return run_goose(
            recipe_path=recipe_path,
            session_name=session_name,
            params=params,
            max_turns=max_turns,
            timeout_secs=timeout_secs,
            model=model,
            provider=provider,
            cwd=cwd,
            memory_limit=memory_limit,
        )

    attempt = 0
    while True:
        attempt += 1
        result = run_goose(
            recipe_path=recipe_path,
            session_name=session_name,
            params=params,
            max_turns=max_turns,
            timeout_secs=timeout_secs,
            model=model,
            provider=provider,
            cwd=cwd,
            memory_limit=memory_limit,
        )

        # --- Success: return immediately ---
        if result.success:
            return result

        # --- Timeout: return immediately (has its own handling) ---
        if result.timed_out:
            return result

        # --- Is this a transient error we should retry? ---
        is_conn_err = is_connection_error(result.raw_stderr, result.return_code)
        is_silent = is_silent_crash(result)

        if not is_conn_err and not is_silent:
            # Genuine non-transient failure — return immediately
            return result

        # --- Transient error detected (connection or silent crash) ---
        error_kind = "connection_error" if is_conn_err else "silent_crash"
        if attempt >= rate_limit.max_retries:
            log.warning(
                "goose.backoff.exhausted",
                session=session_name,
                attempt=attempt,
                max_retries=rate_limit.max_retries,
                error_kind=error_kind,
                stderr=result.raw_stderr[:200],
            )
            break  # exit primary retry loop → try fallback

        delay = rate_limit.next_delay(attempt)
        log.warning(
            "goose.backoff.retry",
            session=session_name,
            attempt=attempt,
            max_retries=rate_limit.max_retries,
            delay_secs=round(delay, 1),
            error_kind=error_kind,
            stderr=result.raw_stderr[:200],
        )
        time.sleep(delay)

    # ── Fallback model ──────────────────────────────────────────
    # Primary model exhausted its retries.  Try the fallback model
    # if one is configured — this keeps progress going even when the
    # primary provider is down for an extended period.
    if fallback_model and fallback_provider:
        log.warning(
            "goose.fallback.attempt",
            session=session_name,
            primary_model=model,
            primary_provider=provider,
            fallback_model=fallback_model,
            fallback_provider=fallback_provider,
        )
        fallback_result = run_goose(
            recipe_path=recipe_path,
            session_name=session_name,
            params=params,
            max_turns=max_turns,
            timeout_secs=timeout_secs,
            model=fallback_model,
            provider=fallback_provider,
            cwd=cwd,
            memory_limit=memory_limit,
        )
        if fallback_result.success:
            log.info(
                "goose.fallback.success",
                session=session_name,
                fallback_model=fallback_model,
                fallback_provider=fallback_provider,
            )
            return fallback_result
        log.warning(
            "goose.fallback.failed",
            session=session_name,
            fallback_model=fallback_model,
            fallback_provider=fallback_provider,
            return_code=fallback_result.return_code,
        )
        return fallback_result

    return result
