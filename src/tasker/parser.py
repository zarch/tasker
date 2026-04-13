"""Markdown task-list parser.

Understands the structure of specs/arch/99-todo.md style files:
  ## Phase 1 — Title
  ### Sub-section
  - [ ] Task text
  - [x] Completed task
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Phase, Task


_PHASE_RE = re.compile(r"^##\s+(?:Phase\s+)?(\d+)[^\n]*$", re.IGNORECASE)
_TASK_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+)$")


def parse_task_file(path: str | Path) -> list[Phase]:
    """Parse a markdown file into a list of Phases with Tasks."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Task file not found: {path}")

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    phases: list[Phase] = []
    current_phase: Phase | None = None
    task_counter = 0

    for line in lines:
        # ── Phase heading ──
        m = _PHASE_RE.match(line.strip())
        if m:
            idx = int(m.group(1)) - 1  # 0-based
            current_phase = Phase(index=idx, title=line.strip().lstrip("# ").strip())
            phases.append(current_phase)
            continue

        # ── Task checkbox ──
        m = _TASK_RE.match(line.strip())
        if m and current_phase is not None:
            done = m.group(1).lower() == "x"
            task = Task(
                phase_index=current_phase.index,
                task_index=task_counter,
                text=m.group(2).strip(),
                done=done,
            )
            current_phase.tasks.append(task)
            task_counter += 1
            continue

    if not phases:
        raise ValueError(f"No phases found in {path}. Expected '## Phase N' headings.")

    return phases


def find_next_task(phases: list[Phase]) -> tuple[Phase, Task] | None:
    """Return the first (phase, task) pair that is not yet done."""
    for phase in phases:
        for task in phase.tasks:
            if not task.done:
                return phase, task
    return None


def mark_task_done(task: Task, phases: list[Phase]) -> None:
    """Mark a task as done in the in-memory model."""
    task.done = True


def update_markdown(path: str | Path, phases: list[Phase]) -> None:
    """Rewrite the markdown file, reflecting done/undone checkboxes."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    task_idx = 0

    for i, line in enumerate(lines):
        m = _TASK_RE.match(line.strip())
        if m:
            phase_idx = _phase_index_for_task(phases, task_idx)
            if phase_idx is not None:
                task = _task_at(phases, phase_idx, task_idx)
                if task is not None:
                    check = "x" if task.done else " "
                    lines[i] = re.sub(
                        r"^(\s*-\s+\[)[ xX](\]\s+)",
                        rf"\g<1>{check}\2",
                        line,
                    )
            task_idx += 1

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _phase_index_for_task(phases: list[Phase], global_idx: int) -> int | None:
    """Map a global task index to its phase index."""
    cumulative = 0
    for phase in phases:
        if global_idx < cumulative + len(phase.tasks):
            return phase.index
        cumulative += len(phase.tasks)
    return None


def _task_at(phases: list[Phase], phase_idx: int, global_idx: int) -> Task | None:
    """Get the task at a global index within a specific phase."""
    cumulative = 0
    for phase in phases:
        if phase.index == phase_idx:
            local_idx = global_idx - cumulative
            if 0 <= local_idx < len(phase.tasks):
                return phase.tasks[local_idx]
            return None
        cumulative += len(phase.tasks)
    return None
