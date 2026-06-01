"""Memory tracking for tasker — lightweight, stdlib-only system memory monitoring.

Reads ``/proc/meminfo`` and ``/proc/<pid>/status`` (Linux-only) to provide:

* **System-level** snapshots: total RAM, available RAM, swap usage.
* **Process-level** snapshots: tasker's own RSS and VmSize, plus child
  process (goose) RSS/VmSize when a PID is provided.
* **Snapshots** that are cheap ``dict`` objects suitable for logging with
  structlog.
* **A rolling delta** between snapshots so callers can report memory growth.

No external dependencies (no psutil).  Uses only ``/proc`` filesystem
which is always available on Linux.

Design Decisions
----------------
* **Lazy import** — nothing in this module imports at module level so it
  can be imported unconditionally without cost.
* **Graceful fallback** — all public functions return ``None`` or empty
  dicts on non-Linux systems or when ``/proc`` is unavailable, so callers
  never need to guard.
* **Structlog-friendly** — ``snapshot()`` returns a flat dict that can be
  passed directly as structlog keyword arguments.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass
class SystemMemory:
    """System-wide memory stats from /proc/meminfo (values in MB)."""

    total_mb: float
    available_mb: float
    used_mb: float
    swap_total_mb: float
    swap_free_mb: float
    swap_used_mb: float
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def available_pct(self) -> float:
        """Percentage of RAM that is available (0–100)."""
        if self.total_mb == 0:
            return 0.0
        return (self.available_mb / self.total_mb) * 100

    @property
    def swap_used_pct(self) -> float:
        """Percentage of swap that is used (0–100)."""
        if self.swap_total_mb == 0:
            return 0.0
        return (self.swap_used_mb / self.swap_total_mb) * 100


@dataclass
class ProcessMemory:
    """Process-level memory stats from /proc/<pid>/status (values in MB)."""

    pid: int
    rss_mb: float
    vm_size_mb: float
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class MemorySnapshot:
    """Combined snapshot: system + tasker process + optional child process."""

    system: SystemMemory
    tasker: ProcessMemory
    child: ProcessMemory | None = None

    def to_dict(self) -> dict[str, Any]:
        """Flatten into a dict suitable for structlog keyword args."""
        d: dict[str, Any] = {
            "sys_total_mb": round(self.system.total_mb, 0),
            "sys_avail_mb": round(self.system.available_mb, 0),
            "sys_avail_pct": round(self.system.available_pct, 1),
            "sys_used_mb": round(self.system.used_mb, 0),
            "swap_used_mb": round(self.system.swap_used_mb, 0),
            "swap_used_pct": round(self.system.swap_used_pct, 1),
            "tasker_rss_mb": round(self.tasker.rss_mb, 1),
            "tasker_vm_mb": round(self.tasker.vm_size_mb, 1),
        }
        if self.child is not None:
            d["child_pid"] = self.child.pid
            d["child_rss_mb"] = round(self.child.rss_mb, 1)
            d["child_vm_mb"] = round(self.child.vm_size_mb, 1)
        return d


# ── Internal helpers ────────────────────────────────────────────────────────

_PROC_MEMINFO = Path("/proc/meminfo")
_PROC_SELF_STATUS = Path("/proc/self/status")


def _read_proc_meminfo() -> dict[str, int]:
    """Parse /proc/meminfo into a dict of key→kB value.

    Returns empty dict on failure (non-Linux, permission error, etc.).
    """
    info: dict[str, int] = {}
    try:
        with open(_PROC_MEMINFO) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    # e.g. "MemTotal:       32548892 kB"
                    key = parts[0].rstrip(":")
                    info[key] = int(parts[1])
    except (OSError, ValueError):
        pass
    return info


def _read_proc_status(pid: int) -> dict[str, int]:
    """Parse /proc/<pid>/status for VmRSS and VmSize (values in kB).

    Returns empty dict on failure.
    """
    status: dict[str, int] = {}
    try:
        with open(Path(f"/proc/{pid}/status")) as f:
            for line in f:
                if line.startswith("VmRSS:") or line.startswith("VmSize:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(":")
                        status[key] = int(parts[1])
    except (OSError, ValueError, ProcessLookupError):
        pass
    return status


def _kb_to_mb(kb: int) -> float:
    return kb / 1024.0


# ── Public API ──────────────────────────────────────────────────────────────


def get_system_memory() -> SystemMemory | None:
    """Return current system-wide memory stats, or None on failure."""
    info = _read_proc_meminfo()
    if not info or "MemTotal" not in info:
        return None

    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)

    return SystemMemory(
        total_mb=_kb_to_mb(total),
        available_mb=_kb_to_mb(available),
        used_mb=_kb_to_mb(total - available),
        swap_total_mb=_kb_to_mb(swap_total),
        swap_free_mb=_kb_to_mb(swap_free),
        swap_used_mb=_kb_to_mb(swap_total - swap_free),
    )


def get_process_memory(pid: int | None = None) -> ProcessMemory | None:
    """Return memory stats for a process, or None on failure.

    If *pid* is None, uses the current process (``os.getpid()``).
    """
    if pid is None:
        pid = os.getpid()

    status = _read_proc_status(pid)
    if not status:
        return None

    return ProcessMemory(
        pid=pid,
        rss_mb=_kb_to_mb(status.get("VmRSS", 0)),
        vm_size_mb=_kb_to_mb(status.get("VmSize", 0)),
    )


def snapshot(child_pid: int | None = None) -> MemorySnapshot | None:
    """Take a combined memory snapshot (system + tasker + optional child).

    Returns None if the system stats cannot be read (non-Linux OS).
    """
    sys_mem = get_system_memory()
    if sys_mem is None:
        return None

    tasker_mem = get_process_memory()
    if tasker_mem is None:
        return None

    child_mem = None
    if child_pid is not None:
        child_mem = get_process_memory(child_pid)

    return MemorySnapshot(system=sys_mem, tasker=tasker_mem, child=child_mem)


def delta(before: MemorySnapshot, after: MemorySnapshot) -> dict[str, float]:
    """Compute the change in memory between two snapshots (after - before).

    Returns a flat dict of key→delta in MB (positive = growth).
    """
    return {
        "sys_avail_delta_mb": round(
            after.system.available_mb - before.system.available_mb, 1
        ),
        "sys_used_delta_mb": round(after.system.used_mb - before.system.used_mb, 1),
        "swap_used_delta_mb": round(
            after.system.swap_used_mb - before.system.swap_used_mb, 1
        ),
        "tasker_rss_delta_mb": round(after.tasker.rss_mb - before.tasker.rss_mb, 1),
    }


def format_snapshot_human(snap: MemorySnapshot) -> str:
    """Format a snapshot as a short human-readable string for UI display.

    Example: ``RAM 4.2G/31G (14%) | Swap 1.8G/31G | Tasker 45M | Goose 890M``
    """
    sys_g = _fmt_gb(snap.system.used_mb)
    sys_t = _fmt_gb(snap.system.total_mb)
    avail_pct = snap.system.available_pct
    swap_u = _fmt_gb(snap.system.swap_used_mb)
    swap_t = _fmt_gb(snap.system.swap_total_mb)
    tasker = _fmt_mb(snap.tasker.rss_mb)

    parts = [
        f"RAM {sys_g}/{sys_t} ({avail_pct:.0f}% avail)",
        f"Swap {swap_u}/{swap_t}",
        f"Tasker {tasker}",
    ]

    if snap.child is not None:
        child = _fmt_mb(snap.child.rss_mb)
        parts.append(f"Goose {child}")

    return " | ".join(parts)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fmt_mb(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    if mb >= 1:
        return f"{mb:.0f}M"
    return f"{mb * 1024:.0f}K"


def _fmt_gb(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb:.0f}M"
