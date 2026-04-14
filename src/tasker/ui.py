"""Rich progress UI — dual progress bars + actor indicator + live table."""

from __future__ import annotations

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text
from rich.layout import Layout

from .models import Actor, IterationEntry, Phase, TaskStatus

import structlog

log = structlog.get_logger(__name__)


class TaskerUI:
    """Live rich console UI for the tasker pipeline."""

    def __init__(self) -> None:
        self.console = Console()
        self._live: Live | None = None
        self._layout: Layout | None = None

        # Progress bars
        self.project_progress = Progress(
            TextColumn("[bold blue]Project {task.description}"),
            BarColumn(bar_width=40),
            TextColumn("{task.completed}/{task.total} phases"),
            TimeElapsedColumn(),
        )
        self.phase_progress = Progress(
            TextColumn("[bold green]Phase   {task.description}"),
            BarColumn(bar_width=40),
            TextColumn("{task.completed}/{task.total} tasks"),
            TimeElapsedColumn(),
        )

        # Task IDs for progress bars (set during init_progress)
        self._project_task_id: TaskID = TaskID(0)
        self._phase_task_id: TaskID = TaskID(0)

        # Accumulated iteration entries for the live table
        self._iteration_entries: list[IterationEntry] = []

    def start(self) -> Live:
        log.debug("ui.live_started")
        self._layout = self._build_layout()
        self._live = Live(self._layout, console=self.console, refresh_per_second=4)
        self._live.start()
        return self._live

    def stop(self) -> None:
        log.debug("ui.live_stopped")
        if self._live:
            self._live.stop()
            self._live = None
        self._layout = None

    def pause(self) -> None:
        """Temporarily stop Live so interactive input works."""
        log.debug("ui.live_paused")
        if self._live:
            self._live.stop()
            self._live = None

    def resume(self) -> None:
        """Recreate Live after interactive input is done."""
        log.debug("ui.live_resumed")
        if self._layout:
            self._live = Live(self._layout, console=self.console, refresh_per_second=4)
            self._live.start()

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="progress", size=8),
            Layout(name="table", ratio=1),
        )
        layout["header"].update(self._actor_panel())
        layout["progress"].split_row(
            Layout(self.project_progress),
            Layout(self.phase_progress),
        )
        layout["table"].update(Panel(self._iteration_table(), title="Iteration Log"))
        return layout

    def _actor_panel(self) -> Panel:
        return Panel("Starting…", title="tasker")

    def _refresh(self) -> None:
        """Push the current layout to Live."""
        if self._live and self._layout:
            self._live.update(self._layout)

    def update_project(self, phases: list[Phase], current_phase: Phase | None) -> None:
        if not self._layout:
            return
        total_phases = len(phases)
        completed_phases = sum(1 for p in phases if p.is_complete)
        current_title = current_phase.title if current_phase else "—"
        self.project_progress.update(
            self._project_task_id,
            description=f"  {current_title[:50]}",
            completed=completed_phases,
            total=total_phases,
        )
        self._refresh()

    def update_phase(self, phase: Phase) -> None:
        if not self._layout:
            return
        self.phase_progress.update(
            self._phase_task_id,
            description=f"  {phase.title[:50]}",
            completed=phase.completed,
            total=phase.total,
        )
        self._refresh()

    def update_actor(self, actor: Actor, task_label: str, detail: str = "") -> None:
        if not self._layout:
            return
        icon = "🧪" if actor == Actor.QA else "🛠️"
        name = "QA Reviewer" if actor == Actor.QA else "Developer"
        color = "magenta" if actor == Actor.QA else "cyan"
        text = Text(f" {icon} {name}  —  Task {task_label}")
        if detail:
            text.append(f"  ({detail})", style="dim")
        panel = Panel(text, title="tasker", style=color)
        self._layout["header"].update(panel)
        self._refresh()

    def add_iteration(self, entry: IterationEntry) -> None:
        """Add a row to the iteration table."""
        self._iteration_entries.append(entry)
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self._iteration_table()
        status_colors = {
            TaskStatus.APPROVED: "green",
            TaskStatus.FEEDBACK: "red",
            TaskStatus.IN_PROGRESS: "yellow",
            TaskStatus.ASSIGNED: "blue",
            TaskStatus.ERROR: "bold red",
        }

        for entry in self._iteration_entries[-20:]:  # show last 20
            status_color = status_colors.get(entry.status, "white")
            actor_str = "[QA]" if entry.actor == Actor.QA else "[DEV]"
            summary = ""
            if entry.payload:
                if entry.actor == Actor.DEV:
                    summary = entry.payload.get("summary", "")[:60]
                elif entry.actor == Actor.QA:
                    summary = entry.payload.get("decision", "")[:60]
                    if entry.payload.get("feedback"):
                        summary += f" — {entry.payload['feedback'][:40]}"

            table.add_row(
                f"#{entry.iteration}",
                entry.timestamp.split("T")[1].split(".")[0]
                if "T" in entry.timestamp
                else entry.timestamp,
                f"[{status_color}]{actor_str}[/{status_color}]",
                entry.task_label,
                f"[{status_color}]{entry.status.value}[/{status_color}]",
                summary[:80],
            )

        if self._layout:
            self._layout["table"].update(Panel(table, title="Iteration Log"))
            self._refresh()

    def _iteration_table(self) -> Table:
        table = Table(
            title="Recent Iterations",
            show_lines=False,
            expand=True,
            box=None,
            row_styles=["dim", ""],
        )
        table.add_column("#", style="dim", width=4)
        table.add_column("Time", style="dim", width=10)
        table.add_column("Actor", width=8)
        table.add_column("Task", width=10)
        table.add_column("Status", width=14)
        table.add_column("Summary", max_width=80)
        return table

    def init_progress(self) -> None:
        self._project_task_id = self.project_progress.add_task(
            "Project", completed=0, total=1
        )
        self._phase_task_id = self.phase_progress.add_task(
            "Phase", completed=0, total=1
        )

    def print_error(self, msg: str) -> None:
        self.console.print(f"[bold red]ERROR:[/bold red] {msg}")

    def print_success(self, msg: str) -> None:
        self.console.print(f"[bold green]✓[/bold green] {msg}")

    def print_info(self, msg: str) -> None:
        self.console.print(f"[blue]ℹ[/blue] {msg}")

    def print_warning(self, msg: str) -> None:
        self.console.print(f"[yellow]⚠[/yellow] {msg}")

    # ── Interactive chat mode ────────────────────────────────────

    def print_chat_header(self, task_label: str, question: str) -> None:
        """Print the header when entering interactive chat mode."""
        self.console.print()
        self.console.rule(
            f"[bold magenta]💬 Interactive Chat — {task_label}[/bold magenta]"
        )
        self.console.print(
            "[magenta]QA has a question that requires your input:[/magenta]\n"
        )
        self.console.print(f"  {question}")
        self.console.print()
        self.console.print(
            "[dim]Type your answer and press Enter. "
            "The QA agent will process your response.\n"
            "Type /done when you believe the issue is resolved to exit chat mode.\n"
            "Type /skip to mark the task as done and move on.[/dim]"
        )
        self.console.print()

    def print_qa_chat_message(self, message: str) -> None:
        """Print a message from the QA agent during chat mode."""
        self.console.print(f"[bold magenta]🧪 QA:[/bold magenta] {message}")

    def prompt_user_input(self) -> str | None:
        """Prompt the user for textual input. Returns None on /skip."""
        try:
            user_input = input("[bold green]👤 You:[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            self.console.print("\n[dim]Input interrupted.[/dim]")
            return None

        if user_input.lower() == "/skip":
            return None  # signal to skip/exit chat
        if user_input.lower() == "/done":
            return ""  # empty string signals "resolved"
        return user_input

    def print_chat_resolved(self) -> None:
        """Print confirmation that the chat issue was resolved."""
        self.console.print()
        self.console.print(
            "[bold green]✓ Issue resolved. Continuing pipeline...[/bold green]"
        )
        self.console.rule()
        self.console.print()

    def print_chat_skipped(self) -> None:
        """Print that the user chose to skip."""
        self.console.print()
        self.console.print("[yellow]Task skipped by user.[/yellow]")
        self.console.rule()
        self.console.print()
