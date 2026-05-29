"""``tasker prepare`` — validate JSONL task files and convert to markdown.

Subcommands::

    tasker prepare validate  <file.tasks.jsonl>   # check structure
    tasker prepare to-md    <file.tasks.jsonl>    # generate 99-todo.md
    tasker prepare to-jsonl <99-todo.md>          # extract tasks from existing MD
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .taskfile import (
    MAX_TASKS_PER_SUBPHASE,
    MAX_SUBPHASES_PER_PHASE,
    TaskEntry,
    read_task_entries,
    validate,
)

prepare_app = typer.Typer(
    name="prepare",
    help="Validate and convert JSONL task files for the tasker orchestrator.",
    no_args_is_help=True,
)
console = Console()


# ── MD → TaskEntry conversion ────────────────────────────────────


def _read_entries(path: Path) -> list[TaskEntry]:
    """Read task entries from either a .tasks.jsonl or a .md file.

    Dispatches based on file suffix so all ``prepare`` subcommands
    accept both formats transparently.
    """
    if path.suffix == ".md" or not path.suffix.endswith("jsonl"):
        return _md_to_entries(path)
    return read_task_entries(path)


def _md_to_entries(path: Path) -> list[TaskEntry]:
    """Convert a markdown task file into a list of :class:`TaskEntry` objects."""
    from .parser import parse_task_file

    phases = parse_task_file(str(path))

    entries: list[TaskEntry] = []
    for phase in phases:
        for task in phase.tasks:
            # Try to extract ref from text
            m = re.search(r"\(Ref:\s*([^)]+)\)", task.text)
            ref = m.group(1).strip() if m else ""
            # Clean text: remove bold prefix and ref
            text = re.sub(r"\s*\(Ref:\s*[^)]+\)", "", task.text).strip()
            id_match = re.match(r"\*\*(\S+)\*\*\s*(.*)", text, re.DOTALL)
            if id_match:
                task_id = id_match.group(1)
                text = id_match.group(2).strip()
            else:
                task_id = f"P{task.phase_index + 1}.T{task.task_index + 1}"

            entries.append(
                TaskEntry(
                    phase=phase.index + 1,
                    phase_title=phase.title,
                    subphase=task.subphase
                    if task.subphase
                    else f"P{phase.index + 1} Tasks",
                    task_id=task_id,
                    text=text,
                    ref=ref,
                    depends_on=[],
                    done=task.done,
                )
            )
    return entries


# ── example ──────────────────────────────────────────────────────


@prepare_app.command("example")
def example_cmd() -> None:
    """Print a valid example JSONL entry with field descriptions."""
    example = {
        "phase": 1,
        "phase_title": "GeometryType Enum",
        "subphase": "P1 Tasks",
        "task_id": "T0.1",
        "text": "Add serde.workspace = true to hay-vector/Cargo.toml.",
        "ref": "00-spec.md#T0.1",
        "depends_on": [],
        "done": False,
    }
    console.print(
        "[bold]Example JSONL entry (one line per task in 99-todo.tasks.jsonl):[/bold]\n"
    )
    console.print(json.dumps(example, ensure_ascii=False))
    console.print("")
    console.print("[bold]Field rules:[/bold]")
    console.print(
        "  [cyan]phase[/cyan]         int       — 1-based, sequential across the file (1, 2, 3, …)"
    )
    console.print(
        "  [cyan]phase_title[/cyan]   str       — phase heading text, e.g. 'GeometryType Enum'"
    )
    console.print(
        "  [cyan]subphase[/cyan]      str       — sub-heading text, e.g. 'P1 Tasks'"
    )
    console.print(
        "  [cyan]task_id[/cyan]       str       — globally unique, e.g. 'T0.1', 'T2A.3', 'TDoc.5'"
    )
    console.print(
        "  [cyan]text[/cyan]          str       — concise but complete task description. No hard"
    )
    console.print(
        "                              length limit; write enough that an external developer"
    )
    console.print(
        "                              understands what to do and where it ends."
    )
    console.print(
        "  [cyan]ref[/cyan]           str       — spec anchor with full design details, code"
    )
    console.print(
        "                              examples, and acceptance criteria, e.g. '00-spec.md#T0.1'"
    )
    console.print(
        "  [cyan]depends_on[/cyan]    list[str] — only for non-obvious dependencies: cross-phase,"
    )
    console.print(
        "                              cross-subphase, or backward references. Omit or use []"
    )
    console.print(
        "                              when the task simply follows the previous one in order."
    )
    console.print("  [cyan]done[/cyan]          bool      — false for new tasks")
    console.print("")
    console.print("[bold]Task quality rules:[/bold]")
    console.print(
        "  • Each task must be [bold]atomic[/bold] — one clear scope, one deliverable."
    )
    console.print(
        "  • [bold]text + ref together[/bold] must be self-contained — an external developer"
    )
    console.print(
        "    must be able to implement the task by reading only those two fields."
    )
    console.print(
        "  • Each task must have a [bold]clear boundary[/bold] — what files to touch,"
    )
    console.print("    what to add/change, and where it ends.")
    console.print(
        "  • The [cyan]ref[/cyan] field must point to the exact spec section with the"
    )
    console.print("    full design details, code examples, and acceptance criteria.")
    console.print(
        "  • Use [cyan]depends_on[/cyan] to express ordering constraints between tasks"
    )
    console.print("    within the same phase (e.g. T2C.2 depends on T2C.1).")
    console.print("")
    console.print("[bold]Limits:[/bold]  max 10 tasks/subphase, max 6 subphases/phase")
    console.print("")
    console.print("[dim]Full workflow:[/dim]")
    console.print(
        "[dim]  1. Create 99-todo.tasks.jsonl (one JSON object per line)[/dim]"
    )
    console.print("[dim]  2. tasker prepare validate 99-todo.tasks.jsonl[/dim]")
    console.print("[dim]  3. tasker prepare to-md 99-todo.tasks.jsonl[/dim]")


# ── validate ──────────────────────────────────────────────────────


@prepare_app.command("validate")
def validate_cmd(
    task_file: Path = typer.Argument(
        ...,
        help="Path to a .tasks.jsonl file to validate.",
        exists=True,
    ),
    max_tasks: int = typer.Option(
        MAX_TASKS_PER_SUBPHASE,
        "--max-tasks",
        help="Maximum tasks allowed per subphase.",
    ),
    max_subphases: int = typer.Option(
        MAX_SUBPHASES_PER_PHASE,
        "--max-subphases",
        help="Maximum subphases allowed per phase.",
    ),
) -> None:
    """Validate a .tasks.jsonl or .md task file against the tasker schema."""
    try:
        entries = _read_entries(task_file)
    except ValueError as exc:
        console.print(f"[bold red]Parse error:[/bold red] {exc}")
        raise typer.Exit(1)

    errors = validate(
        entries,
        max_tasks_per_subphase=max_tasks,
        max_subphases_per_phase=max_subphases,
    )

    if errors:
        console.print(f"[bold red]{len(errors)} validation error(s):[/bold red]\n")
        for err in errors:
            console.print(f"  [red]✗[/red] {err}")
        raise typer.Exit(1)

    # Summary
    phases_seen: dict[int, str] = {}
    subphase_counts: dict[tuple[int, str], int] = {}
    for e in entries:
        phases_seen[e.phase] = e.phase_title
        subphase_counts[(e.phase, e.subphase)] = (
            subphase_counts.get((e.phase, e.subphase), 0) + 1
        )

    table = Table(title=f"✓ Valid: {len(entries)} tasks")
    table.add_column("Phase", style="bold")
    table.add_column("Title")
    table.add_column("Subphase")
    table.add_column("Tasks", justify="right")

    for phase_num in sorted(phases_seen):
        phase_subs = {k: v for k, v in subphase_counts.items() if k[0] == phase_num}
        for i, ((_, sub_name), count) in enumerate(sorted(phase_subs.items())):
            table.add_row(
                str(phase_num) if i == 0 else "",
                phases_seen[phase_num] if i == 0 else "",
                sub_name,
                str(count),
            )

    console.print(table)


# ── to-md ─────────────────────────────────────────────────────────


@prepare_app.command("to-md")
def to_md(
    task_file: Path = typer.Argument(
        ...,
        help="Path to a .tasks.jsonl file.",
        exists=True,
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output markdown path (default: <stem>.md next to the JSONL file).",
    ),
    title: str = typer.Option(
        "Implementation TODO",
        "--title",
        help="Document title for the generated markdown.",
    ),
) -> None:
    """Convert a .tasks.jsonl or .md file to a markdown task list."""
    try:
        entries = _read_entries(task_file)
    except ValueError as exc:
        console.print(f"[bold red]Parse error:[/bold red] {exc}")
        raise typer.Exit(1)

    errors = validate(entries)
    if errors:
        console.print(
            f"[bold red]Cannot generate MD — {len(errors)} validation error(s):[/bold red]"
        )
        for err in errors:
            console.print(f"  [red]✗[/red] {err}")
        raise typer.Exit(1)

    md = _entries_to_md(entries, title)
    out_path = output or task_file.with_suffix(".md")
    out_path.write_text(md, encoding="utf-8")
    console.print(f"[green]✓[/green] Wrote {out_path} ({len(entries)} tasks)")


def _entries_to_md(entries: list[TaskEntry], title: str) -> str:
    """Render task entries as a markdown file matching the parser's expected format."""
    lines: list[str] = [f"# {title}\n"]

    current_phase = 0
    current_subphase = ""

    for entry in entries:
        # Phase heading
        if entry.phase != current_phase:
            current_phase = entry.phase
            current_subphase = ""
            lines.append(f"\n## Phase {entry.phase} — {entry.phase_title}\n")

        # Subphase heading
        if entry.subphase != current_subphase:
            current_subphase = entry.subphase
            lines.append(f"\n### {entry.subphase}\n")

        # Task checkbox
        check = "x" if entry.done else " "
        ref_part = f" (Ref: {entry.ref})" if entry.ref else ""
        lines.append(f"- [{check}] **{entry.task_id}** {entry.text}{ref_part}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


# ── to-jsonl ──────────────────────────────────────────────────────


@prepare_app.command("to-jsonl")
def to_jsonl(
    task_file: Path = typer.Argument(
        ...,
        help="Path to a markdown task list (## Phase N format).",
        exists=True,
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output JSONL path (default: <stem>.tasks.jsonl next to the MD file).",
    ),
) -> None:
    """Extract tasks from an existing markdown file into JSONL format."""
    try:
        entries = _md_to_entries(task_file)
    except ValueError as exc:
        console.print(f"[bold red]Parse error:[/bold red] {exc}")
        raise typer.Exit(1)

    errors = validate(entries)
    if errors:
        console.print(
            f"[bold yellow]Warning:[/bold yellow] {len(errors)} validation issue(s):"
        )
        for err in errors:
            console.print(f"  [yellow]⚠[/yellow] {err}")

    out_path = output or task_file.with_suffix(".tasks.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    console.print(f"[green]✓[/green] Wrote {out_path} ({len(entries)} tasks)")
