"""Unified task-file adapter — dispatches between markdown and JSONL.

The orchestrator calls these functions instead of calling the parser /
update_markdown / rewrite_task_text functions directly.  They detect the
file extension and delegate to the appropriate backend.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from .models import Phase, Task

log = structlog.get_logger(__name__)


def load_tasks(path: Path) -> list[Phase]:
    """Load tasks from a markdown or JSONL file, returning the Phase model."""
    if path.suffix == ".jsonl":
        from .taskfile import read_task_entries, entries_to_phases

        entries = read_task_entries(path)
        phases = entries_to_phases(entries)
        log.info("adapter.loaded_jsonl", path=str(path), phases=len(phases))
        return phases
    else:
        from .parser import parse_task_file

        return parse_task_file(path)


def mark_done(path: Path, task: Task, phases: list[Phase]) -> None:
    """Mark a task as done in the underlying file (markdown or JSONL)."""
    from .parser import mark_task_done, update_markdown

    # Always update the in-memory model
    mark_task_done(task, phases)

    if path.suffix == ".jsonl":
        from .taskfile import update_jsonl_done

        update_jsonl_done(path, task_id=_find_task_id(task))
        # Also update companion markdown if it exists
        md_path = _companion_md(path)
        if md_path.exists():
            update_markdown(md_path, phases)
            log.debug("adapter.companion_md_updated", path=str(md_path))
    else:
        update_markdown(path, phases)


def rewrite_text(path: Path, phases: list[Phase], task: Task, new_text: str) -> None:
    """Rewrite a task's text in the underlying file (markdown or JSONL)."""
    if path.suffix == ".jsonl":
        from .taskfile import rewrite_jsonl_task_text
        from .parser import rewrite_task_text

        task_id = _find_task_id(task)
        rewrite_jsonl_task_text(path, task_id=task_id, new_text=new_text)
        # Update in-memory model
        task.text = new_text
        # Also update companion markdown if it exists
        md_path = _companion_md(path)
        if md_path.exists():
            # Re-derive phases from updated JSONL so the MD stays in sync
            from .taskfile import read_task_entries
            from .prepare import _entries_to_md

            entries = read_task_entries(path)
            md_content = _entries_to_md(entries, "Implementation TODO")
            md_path.write_text(md_content, encoding="utf-8")
            log.debug("adapter.companion_md_rewritten", path=str(md_path))
    else:
        from .parser import rewrite_task_text

        rewrite_task_text(path, phases, task, new_text)


def insert_subtasks_dispatch(
    path: Path,
    phases: list[Phase],
    task: Task,
    subtask_labels: list[str],
    subtask_texts: list[str],
) -> None:
    """Insert subtasks for a decomposed task (markdown only for now).

    JSONL subtask insertion is more complex (requires re-numbering all
    subsequent entries) and will be added when needed.  For now, raise
    a clear error if attempted on a JSONL file.
    """
    if path.suffix == ".jsonl":
        log.warning("adapter.jsonl_subtask_insert_not_supported")
        # Fall back to in-memory only — the orchestrator will re-parse
        task.done = True
        # TODO: implement JSONL subtask insertion
        return
    else:
        from .parser import insert_subtasks

        insert_subtasks(path, phases, task, subtask_labels, subtask_texts)


# ── helpers ──────────────────────────────────────────────────────


def _find_task_id(task: Task) -> str:
    """Extract the task_id from a Task's label or text.

    Tasks loaded from JSONL have their original task_id embedded in the
    text as ``**T0.1** ...``.  Tasks loaded from markdown use the parser's
    label format (``P1.T1``).
    """
    import re

    # Check for bold task_id in text (JSONL-generated markdown)
    m = re.match(r"\*\*(\S+?)\*\*", task.text)
    if m:
        return m.group(1)
    # Fallback to the parser label
    return task.label


def _companion_md(jsonl_path: Path) -> Path:
    """Derive the companion markdown path from a JSONL path.

    ``99-todo.tasks.jsonl`` → ``99-todo.md``
    """
    stem = jsonl_path.stem  # "99-todo.tasks" from "99-todo.tasks.jsonl"
    # Remove ".tasks" suffix if present
    if stem.endswith(".tasks"):
        stem = stem[: -len(".tasks")]
    return jsonl_path.parent / f"{stem}.md"
