"""tasker — Goose-based task orchestration CLI with QA/Dev feedback loop.

Usage:
    uv run python -m tasker --dev recipe-dev.yaml --qa recipe-qa.yaml specs/arch/99-todo.md
    uv run python -m tasker --dev recipe-dev.yaml --qa recipe-qa.yaml specs/arch/99-todo.md --start-phase 3
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .orchestrator import Orchestrator
from .models import SessionScope

# Default recipes shipped with tasker, resolved relative to this file.
_RECIPES_DIR = Path(__file__).resolve().parent.parent.parent / "recipes"
_DEFAULT_DEV = _RECIPES_DIR / "recipe-dev.yaml"
_DEFAULT_QA = _RECIPES_DIR / "recipe-qa.yaml"

app = typer.Typer(
    name="tasker",
    help="Orchestrate goose-based QA/Dev feedback loops from a markdown task list.",
    no_args_is_help=True,
)
console = Console()


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.exists():
        console.print(f"[bold red]Error:[/bold red] File not found: {p}")
        raise typer.Exit(1)
    return p


@app.command()
def main(
    dev: Path = typer.Option(
        _DEFAULT_DEV,
        "--dev",
        help="Path to the developer goose recipe (YAML). Default: recipe-dev.yaml",
    ),
    qa: Path = typer.Option(
        _DEFAULT_QA,
        "--qa",
        help="Path to the QA goose recipe (YAML). Default: recipe-qa.yaml",
    ),
    task_file: Path = typer.Argument(
        ...,
        help="Path to the markdown task list file.",
        exists=True,
    ),
    log_file: Path = typer.Option(
        None,
        "--log",
        help="Path for the JSONL iteration log (default: <task_file>.iterations.jsonl).",
    ),
    max_iterations: int = typer.Option(
        10,
        "--max-iterations",
        help="Max QA↔Dev iterations per task before skipping.",
    ),
    max_turns: int = typer.Option(
        80,
        "--max-turns",
        help="Max goose agent turns per invocation.",
    ),
    timeout: int = typer.Option(
        600,
        "--timeout",
        help="Timeout in seconds for each goose run invocation. Default: 600 (10 minutes). Process is killed and relaunched on timeout.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Override the goose model.",
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Override the goose provider.",
    ),
    start_phase: int | None = typer.Option(
        None,
        "--start-phase",
        help="Start from a specific phase number (1-based). Earlier phases are marked done.",
    ),
    vcs: str = typer.Option(
        "none",
        "--vcs",
        help="VCS integration: 'jj' (Jujutsu), 'git' (feature branch + squash), or 'none' (default).",
    ),
    session_scope: str = typer.Option(
        "subphase",
        "--session-scope",
        help="When to rotate goose sessions: phase (per ## heading), subphase (per ### heading, default), or task (per task).",
    ),
    new_session: bool = typer.Option(
        False,
        "--new-session",
        help="Force creation of a new goose session on the next task (one-shot).",
    ),
) -> None:
    """Run the QA/Dev orchestrator on a markdown task list."""
    # Validate session scope
    valid_scopes = {s.value for s in SessionScope}
    if session_scope not in valid_scopes:
        console.print(
            f"[bold red]Error:[/bold red] Invalid --session-scope '{session_scope}'. "
            f"Must be one of: {', '.join(sorted(valid_scopes))}"
        )
        raise typer.Exit(1)

    # Validate recipe paths (typer's exists=True doesn't work with dynamic defaults)
    for label, path in [("Developer", dev), ("QA", qa)]:
        if not path.exists():
            console.print(
                f"[bold red]Error:[/bold red] {label} recipe not found: {path}\n"
                f"  Use --dev / --qa to specify an alternate path."
            )
            raise typer.Exit(1)

    log_path = log_file or task_file.with_suffix(".iterations.jsonl")

    # Default cwd to the task file's parent so goose agents operate
    # in the correct project context.
    cwd = task_file.resolve().parent

    # Resolve recipe paths to absolute so goose can find them regardless of cwd
    dev_abs = dev.resolve()
    qa_abs = qa.resolve()

    # Resolve VCS backend
    from .vcs import create_backend

    try:
        vcs_backend = create_backend(vcs)
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    orchestrator = Orchestrator(
        task_file=task_file,
        dev_recipe=dev_abs,
        qa_recipe=qa_abs,
        log_file=log_path,
        max_iterations_per_task=max_iterations,
        max_turns=max_turns,
        timeout_secs=timeout,
        model=model,
        provider=provider,
        cwd=cwd,
        start_phase=start_phase,
        vcs=vcs_backend,
        session_scope=SessionScope(session_scope),
        force_new_session=new_session,
    )

    orchestrator.run()


if __name__ == "__main__":
    app()
