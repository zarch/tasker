"""Task-file schema for the JSONL-based task definition format.

Each line in a ``.tasks.jsonl`` file is a JSON object describing one task.
The file is the machine-readable source of truth; ``tasker prepare`` converts
it to a human-readable ``99-todo.md`` and the orchestrator reads it directly.

Schema (one JSON object per line)::

    {
      "phase":       1,                          // int, 1-based, sequential
      "phase_title": "GeometryType Enum",         // str, non-empty
      "subphase":    "P1 Tasks",                  // str, non-empty
      "task_id":     "T0.1",                      // str, globally unique
      "text":        "Add serde.workspace = ...",  // str, non-empty, concise but clear
      "ref":         "00-spec.md#T0.1",           // str, non-empty
      "depends_on":  [],                          // list[str], task_ids that must be done first
      "done":        false                         // bool
    }

Validation rules (enforced by ``tasker prepare validate``):

  1. Phase numbers are 1-based, sequential, no gaps.
  2. ``task_id`` is unique across the entire file.
  3. Max tasks per subphase: ``max_tasks_per_subphase`` (default 10).
  4. Max subphases per phase: ``max_subphases_per_phase`` (default 6).
  5. ``text`` is non-empty (no hard length limit; write enough to be clear).
  6. ``ref`` is non-empty.
  7. ``phase_title`` is non-empty.
  8. ``subphase`` is non-empty.
  9. ``done`` is boolean.
  10. Every ``depends_on`` task_id must exist in the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from .models import Phase

log = structlog.get_logger(__name__)

# Default limits
MAX_TASKS_PER_SUBPHASE = 10
MAX_SUBPHASES_PER_PHASE = 6


@dataclass
class TaskEntry:
    """A single task from a ``.tasks.jsonl`` file."""

    phase: int
    phase_title: str
    subphase: str
    task_id: str
    text: str
    ref: str
    depends_on: list[str] = field(default_factory=list)
    done: bool = False

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "phase_title": self.phase_title,
            "subphase": self.subphase,
            "task_id": self.task_id,
            "text": self.text,
            "ref": self.ref,
            "depends_on": self.depends_on,
            "done": self.done,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TaskEntry:
        return cls(
            phase=d["phase"],
            phase_title=d["phase_title"],
            subphase=d["subphase"],
            task_id=d["task_id"],
            text=d["text"],
            ref=d["ref"],
            depends_on=d.get("depends_on", []),
            done=d.get("done", False),
        )


@dataclass
class ValidationError:
    """A single validation error."""

    line: int  # 1-based line number in the JSONL file
    field: str  # which field has the problem
    message: str  # human-readable error

    def __str__(self) -> str:
        return f"line {self.line}: {self.field}: {self.message}"


def read_task_entries(path: Path) -> list[TaskEntry]:
    """Read all task entries from a ``.tasks.jsonl`` file."""
    entries: list[TaskEntry] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_num}: invalid JSON: {exc}") from exc
            entries.append(TaskEntry.from_dict(obj))
    return entries


def validate(
    entries: list[TaskEntry],
    *,
    max_tasks_per_subphase: int = MAX_TASKS_PER_SUBPHASE,
    max_subphases_per_phase: int = MAX_SUBPHASES_PER_PHASE,
) -> list[ValidationError]:
    """Validate a list of task entries. Returns errors (empty = valid)."""
    errors: list[ValidationError] = []

    # Track seen values for uniqueness / counting
    seen_task_ids: dict[str, int] = {}  # task_id → line
    phase_subphases: dict[int, dict[str, int]] = {}  # phase → {subphase: task_count}
    seen_phases: set[int] = set()

    for i, entry in enumerate(entries):
        line = i + 1

        # Required fields are non-empty
        if not entry.phase_title:
            errors.append(ValidationError(line, "phase_title", "must not be empty"))
        if not entry.subphase:
            errors.append(ValidationError(line, "subphase", "must not be empty"))
        if not entry.text:
            errors.append(ValidationError(line, "text", "must not be empty"))
        if not entry.ref:
            errors.append(ValidationError(line, "ref", "must not be empty"))
        if not entry.task_id:
            errors.append(ValidationError(line, "task_id", "must not be empty"))

        # task_id uniqueness
        if entry.task_id in seen_task_ids:
            errors.append(
                ValidationError(
                    line,
                    "task_id",
                    f"duplicate task_id '{entry.task_id}' (first seen at line {seen_task_ids[entry.task_id]})",
                )
            )
        else:
            seen_task_ids[entry.task_id] = line

        # Track subphase task counts
        if entry.phase not in phase_subphases:
            phase_subphases[entry.phase] = {}
        subphases = phase_subphases[entry.phase]
        subphases[entry.subphase] = subphases.get(entry.subphase, 0) + 1
        seen_phases.add(entry.phase)

    # Phase numbers must be sequential starting from 1
    if seen_phases:
        expected = set(range(1, max(seen_phases) + 1))
        if seen_phases != expected:
            missing = expected - seen_phases
            extra = seen_phases - expected
            if missing:
                errors.append(
                    ValidationError(
                        0, "phase", f"missing phase numbers: {sorted(missing)}"
                    )
                )
            if extra:
                errors.append(
                    ValidationError(
                        0, "phase", f"unexpected phase numbers: {sorted(extra)}"
                    )
                )

    # Max tasks per subphase
    for phase_num, subphases in phase_subphases.items():
        for subphase_name, count in subphases.items():
            if count > max_tasks_per_subphase:
                errors.append(
                    ValidationError(
                        0,
                        "subphase",
                        f"phase {phase_num} subphase '{subphase_name}' has "
                        f"{count} tasks (max {max_tasks_per_subphase})",
                    )
                )

    # Max subphases per phase
    for phase_num, subphases in phase_subphases.items():
        if len(subphases) > max_subphases_per_phase:
            errors.append(
                ValidationError(
                    0,
                    "phase",
                    f"phase {phase_num} has {len(subphases)} subphases "
                    f"(max {max_subphases_per_phase})",
                )
            )

    # depends_on references must exist in the file
    all_task_ids = set(seen_task_ids.keys())
    for i, entry in enumerate(entries):
        for dep in entry.depends_on:
            if dep not in all_task_ids:
                errors.append(
                    ValidationError(
                        i + 1,
                        "depends_on",
                        f"'{dep}' does not match any task_id in the file",
                    )
                )

    return errors


# ── JSONL ↔ orchestrator model bridge ────────────────────────────


def entries_to_phases(entries: list[TaskEntry]) -> list[Phase]:
    """Convert flat JSONL entries into the orchestrator's Phase/Task model.

    Returns a list of :class:`Phase` objects, each containing :class:`Task`
    objects with ``phase_index``, ``task_index``, ``subphase``, etc. set
    correctly — identical to what :func:`parser.parse_task_file` would produce.
    """
    from .models import Phase as MPhase, Task as MTask

    phase_map: dict[int, MPhase] = {}
    task_counter = 0

    for entry in entries:
        if entry.phase not in phase_map:
            phase_map[entry.phase] = MPhase(
                index=entry.phase - 1,  # 0-based
                title=entry.phase_title,
            )
        phase = phase_map[entry.phase]

        subphase_idx = -1
        if entry.subphase:
            # Count how many previous tasks in this phase+subphase
            subphase_idx = sum(1 for t in phase.tasks if t.subphase == entry.subphase)

        task = MTask(
            phase_index=phase.index,
            task_index=task_counter,
            text=entry.text,
            done=entry.done,
            subphase=entry.subphase,
            subphase_index=subphase_idx,
        )
        phase.tasks.append(task)
        task_counter += 1

    # Return phases in order
    return [phase_map[k] for k in sorted(phase_map.keys())]


def update_jsonl_done(path: Path, task_id: str) -> None:
    """Mark a task as done in the JSONL file (by task_id).

    Reads the file line by line, flips ``done`` to ``true`` for the
    matching task_id, writes all lines back.
    """
    lines_out: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                lines_out.append(line)
                continue
            obj = json.loads(stripped)
            if obj.get("task_id") == task_id:
                obj["done"] = True
            lines_out.append(json.dumps(obj, ensure_ascii=False) + "\n")
    path.write_text("".join(lines_out), encoding="utf-8")


def rewrite_jsonl_task_text(path: Path, task_id: str, new_text: str) -> None:
    """Rewrite a task's text in the JSONL file (by task_id).

    Used by the ARCH agent's CLARIFY action.
    """
    lines_out: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                lines_out.append(line)
                continue
            obj = json.loads(stripped)
            if obj.get("task_id") == task_id:
                obj["text"] = new_text
            lines_out.append(json.dumps(obj, ensure_ascii=False) + "\n")
    path.write_text("".join(lines_out), encoding="utf-8")
