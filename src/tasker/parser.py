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

import structlog

from .models import Phase, Task

log = structlog.get_logger(__name__)


_PHASE_EXPLICIT_RE = re.compile(r"^##\s+Phase\s+(\w[\w.-]*)[^\n]*$", re.IGNORECASE)
_PHASE_ANY_RE = re.compile(r"^##\s+(.+)$")
_SUBPHASE_RE = re.compile(r"^#{3,4}\s+(.+)$")
_TASK_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.+)$")


def parse_task_file(path: str | Path) -> list[Phase]:
    """Parse a markdown file into a list of Phases with Tasks.

    Each Phase corresponds to a ``##`` heading — either the explicit
    ``## Phase N — Title`` format or a bare ``## Title`` (as produced
    by ``tasker prepare to-md``).  ``###`` / ``####`` sub-headings are
    recorded on individual Task objects via the ``subphase`` field so
    the orchestrator can compute session-scope keys.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Task file not found: {path}")

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    phases: list[Phase] = []
    current_phase: Phase | None = None
    current_subphase: str = (
        ""  # tracks the latest ### / #### heading text within a phase
    )
    subphase_task_counter: int = 0  # 0-based task index within current ### group
    task_counter = 0

    log.debug("parser.parsing", path=str(path), lines=len(lines))

    for line in lines:
        stripped = line.strip()

        # ── Phase heading (##) ──
        # Accept both "## Phase N — Title" and bare "## Title".
        m = _PHASE_EXPLICIT_RE.match(stripped)
        if m:
            idx = len(phases)
            current_phase = Phase(index=idx, title=stripped.lstrip("# ").strip())
            phases.append(current_phase)
            current_subphase = ""
            subphase_task_counter = 0
            continue

        m = _PHASE_ANY_RE.match(stripped)
        if m:
            idx = len(phases)
            current_phase = Phase(index=idx, title=m.group(1).strip())
            phases.append(current_phase)
            current_subphase = ""
            subphase_task_counter = 0
            continue

        # ── Sub-phase heading (###) ──
        m = _SUBPHASE_RE.match(stripped)
        if m and current_phase is not None:
            current_subphase = m.group(1).strip()
            subphase_task_counter = 0  # reset on each ### heading
            # Also tag the Phase so the orchestrator can see it
            if not current_phase.subphase:
                current_phase.subphase = current_subphase
            continue

        # ── Task checkbox ──
        m = _TASK_RE.match(stripped)
        if m and current_phase is not None:
            done = m.group(1).lower() == "x"
            task = Task(
                phase_index=current_phase.index,
                task_index=task_counter,
                text=m.group(2).strip(),
                done=done,
                subphase=current_subphase,
                subphase_index=subphase_task_counter if current_subphase else -1,
            )
            current_phase.tasks.append(task)
            task_counter += 1
            subphase_task_counter += 1
            continue

    if not phases:
        raise ValueError(f"No phases found in {path}. Expected '## Phase N' headings.")

    total_tasks = sum(len(p.tasks) for p in phases)
    log.info(
        "parser.parsed",
        path=str(path),
        phases=len(phases),
        tasks=total_tasks,
    )

    return phases


def find_next_task(phases: list[Phase]) -> tuple[Phase, Task] | None:
    """Return the first (phase, task) pair that is not yet done."""
    for phase in phases:
        for task in phase.tasks:
            if task.skipped:
                continue
            if not task.done:
                return phase, task
    return None


def mark_task_done(task: Task, phases: list[Phase]) -> None:
    """Mark a task as done in the in-memory model."""
    task.done = True


def update_markdown(path: str | Path, phases: list[Phase]) -> None:
    """Rewrite the markdown file, reflecting done/undone checkboxes."""
    path = Path(path)
    log.debug("parser.updating_markdown", path=str(path))
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


def insert_subtasks(
    path: str | Path,
    phases: list[Phase],
    task: Task,
    subtask_labels: list[str],
    subtask_texts: list[str],
) -> None:
    """Replace a task's checkbox line with multiple subtask checkbox lines.

    The original task is marked done (it's been decomposed). New tasks are
    inserted immediately after it in the markdown and in the in-memory model.

    Args:
        path: Path to the markdown task file.
        phases: The in-memory phase model (will be mutated).
        task: The task being decomposed (will be marked done in-memory).
        subtask_labels: Labels for the new subtasks (for logging/display).
        subtask_texts: Texts for the new subtasks.
    """
    path = Path(path)
    log.debug(
        "parser.inserting_subtasks",
        path=str(path),
        task_label=task.label,
        count=len(subtask_texts),
    )

    # Find the line number of the original task in the markdown
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    target_line = _find_task_line(lines, task)
    if target_line is None:
        log.warning(
            "parser.task_line_not_found",
            task_label=task.label,
            task_text=task.text[:60],
        )
        return

    # Build replacement lines: mark original as done, add new tasks after
    indent = _get_line_indent(lines[target_line])
    new_lines = []
    for label, text_item in zip(subtask_labels, subtask_texts):
        new_lines.append(f"{indent}- [ ] {text_item}")

    # Replace the original task line with: [x] original + new subtask lines
    lines[target_line] = re.sub(
        r"^(\s*-\s+\[)[ xX](\]\s+)",
        r"\g<1>x\2",
        lines[target_line],
    )
    for i, new_line in enumerate(new_lines):
        lines.insert(target_line + 1 + i, new_line)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Update in-memory model
    task.done = True
    phase = phases[task.phase_index]
    insert_at = phase.tasks.index(task) + 1
    for i, (label, text_item) in enumerate(zip(subtask_labels, subtask_texts)):
        new_task = Task(
            phase_index=task.phase_index,
            task_index=-1,  # will be recalculated
            text=text_item,
            done=False,
            subphase=task.subphase,
            subphase_index=task.subphase_index,
        )
        phase.tasks.insert(insert_at + i, new_task)

    # Re-index task_index values for all tasks in this phase
    for idx, t in enumerate(phase.tasks):
        t.task_index = idx

    log.info(
        "parser.subtasks_inserted",
        task_label=task.label,
        count=len(subtask_texts),
    )


def rewrite_task_text(
    path: str | Path,
    phases: list[Phase],
    task: Task,
    new_text: str,
) -> None:
    """Rewrite a task's text in the markdown file and in-memory model.

    Used by the ARCH agent's CLARIFY action to replace an ambiguous task
    description with a more precise one.
    """
    path = Path(path)
    log.debug(
        "parser.rewriting_task_text",
        path=str(path),
        task_label=task.label,
        old_text=task.text[:60],
        new_text=new_text[:60],
    )

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    target_line = _find_task_line(lines, task)
    if target_line is None:
        log.warning(
            "parser.task_line_not_found",
            task_label=task.label,
            task_text=task.text[:60],
        )
        return

    # Replace the text after the checkbox marker
    lines[target_line] = re.sub(
        r"^(\s*-\s+\[[ xX]\]\s+).+$",
        rf"\g<1>{new_text}",
        lines[target_line],
    )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Update in-memory model
    task.text = new_text

    log.info(
        "parser.task_text_rewritten",
        task_label=task.label,
    )


def _find_task_line(lines: list[str], task: Task) -> int | None:
    """Find the line index of a task's checkbox in the markdown.

    Searches for a line matching ``- [ ] <task.text>`` or ``- [x] <task.text>``.
    Returns None if not found (task may have been already modified).
    """
    # Match by task text prefix (task may have been partially modified)
    task_prefix = task.text[:60]
    for i, line in enumerate(lines):
        m = _TASK_RE.match(line.strip())
        if m and m.group(2).strip().startswith(task_prefix):
            return i
    # Fallback: match by exact text
    for i, line in enumerate(lines):
        m = _TASK_RE.match(line.strip())
        if m and m.group(2).strip() == task.text:
            return i
    return None


def _get_line_indent(line: str) -> str:
    """Extract the leading whitespace from a line."""
    stripped = line.lstrip()
    if len(stripped) == len(line):
        return ""
    return line[: len(line) - len(stripped)]
