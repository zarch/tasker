"""Rich progress UI — dual progress bars + actor indicator + live table."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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


# Braille spinner frames — animated dots that convey "running"
_SPINNER_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]


@dataclass
class _PendingIteration:
    """Tracks a running dev/QA call that should appear as a live row in the table."""

    actor: Actor
    task_label: str
    detail: str = ""
    start: float = field(default_factory=time.monotonic)


class _ActivityRenderable:
    """Rich renderable that shows a pulsing elapsed-time indicator.

    Designed to be placed inside the header panel while a goose subprocess
    is running.  It updates via the Live refresh loop (no extra thread
    needed for rendering — we just read ``time.monotonic()`` each refresh).
    """

    __slots__ = ("_start", "_stopped", "_label")

    def __init__(self, label: str) -> None:
        self._start = time.monotonic()
        self._stopped = False
        self._label = label

    def stop(self) -> None:
        self._stopped = True

    @property
    def elapsed_secs(self) -> float:
        return time.monotonic() - self._start

    def __rich_console__(self, console, options):
        elapsed = self.elapsed_secs
        minutes = int(elapsed) // 60
        seconds = int(elapsed) % 60

        # Cycle through animation frames to show liveness
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        frame_idx = int(elapsed * 4) % len(frames)
        spinner = frames[frame_idx] if not self._stopped else "✔"

        elapsed_str = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

        yield Text(
            f"  {spinner} {self._label}  ({elapsed_str})",
            style="bold yellow" if not self._stopped else "bold green",
        )


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

        # Activity indicator state
        self._activity: _ActivityRenderable | None = None
        self._activity_label: str = ""

        # Pending iteration row — shows animated spinner while dev/QA runs
        self._pending: _PendingIteration | None = None

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

    # ── Activity indicator (header panel) ────────────────────────

    def activity_start(self, label: str) -> None:
        """Start a live elapsed-time spinner in the header panel.

        Call ``activity_stop()`` when the work is done.  The Rich Live
        refresh loop will keep the spinner and elapsed time updated
        automatically (no extra thread needed).
        """
        log.debug("ui.activity_started", label=label)
        self._activity_label = label
        self._activity = _ActivityRenderable(label)
        if self._layout:
            self._layout["header"].update(
                Panel(self._activity, title="tasker", style="yellow")
            )
            self._refresh()

    def activity_stop(self) -> float:
        """Stop the activity indicator and return elapsed seconds."""
        if self._activity is not None:
            elapsed = self._activity.elapsed_secs
            self._activity.stop()
            self._activity = None
            log.debug(
                "ui.activity_stopped",
                label=self._activity_label,
                elapsed=round(elapsed, 2),
            )
            self._activity_label = ""
            return elapsed
        return 0.0

    def activity_detail(self, detail: str) -> None:
        """Update the activity label in-place (e.g. add iteration info)."""
        if self._activity is not None:
            self._activity._label = detail
            if self._layout:
                self._refresh()

    # ── Pending iteration indicator (table row) ──────────────────

    def set_pending_iteration(
        self, actor: Actor, task_label: str, detail: str = ""
    ) -> None:
        """Show an animated "running" row in the iteration table.

        Call ``clear_pending_iteration()`` when the goose call returns.
        The Rich Live refresh loop keeps the spinner animated automatically.
        """
        log.debug(
            "ui.pending_started",
            actor=actor.value,
            task_label=task_label,
            detail=detail,
        )
        self._pending = _PendingIteration(
            actor=actor, task_label=task_label, detail=detail
        )
        self._refresh_table()

    def clear_pending_iteration(self) -> None:
        """Remove the animated "running" row from the iteration table."""
        self._pending = None
        self._refresh_table()

    # ── Iteration table ──────────────────────────────────────────

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
            summary = _entry_summary(entry)

            table.add_row(
                f"#{entry.iteration}",
                _format_timestamp(entry.timestamp),
                f"[{status_color}]{actor_str}[/{status_color}]",
                entry.task_label,
                f"[{status_color}]{entry.status.value}[/{status_color}]",
                summary[:80],
            )

        # Animated pending row — shows while dev/QA subprocess is running
        if self._pending is not None:
            p = self._pending
            elapsed = time.monotonic() - p.start
            frame_idx = int(elapsed * 3) % len(_SPINNER_FRAMES)
            spinner = _SPINNER_FRAMES[frame_idx]
            minutes = int(elapsed) // 60
            seconds = int(elapsed) % 60
            elapsed_str = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
            actor_str = "[QA]" if p.actor == Actor.QA else "[DEV]"
            now = time.strftime("%H:%M:%S")
            detail_suffix = f" ({p.detail})" if p.detail else ""

            table.add_row(
                "…",
                now,
                f"[bold yellow]{actor_str}[/bold yellow]",
                p.task_label,
                f"[bold yellow]{spinner} running ({elapsed_str})[/bold yellow]",
                f"[dim]waiting{detail_suffix}[/dim]",
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
        table.add_column("Status", width=20)
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
            "Type /skip to mark the as done and move on.[/dim]"
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


# ── Module-level helpers ──────────────────────────────────────────


def _format_timestamp(ts: str) -> str:
    """Extract HH:MM:SS from an ISO timestamp."""
    if "T" in ts:
        return ts.split("T")[1].split(".")[0][:8]
    return ts[:8]


def _entry_summary(entry: IterationEntry) -> str:
    """Build a human-readable summary string for an iteration entry."""
    if not entry.payload:
        return ""
    if entry.actor == Actor.DEV:
        # Check for error payloads
        error = entry.payload.get("error", "")
        if error:
            if error == "timeout":
                return f"⏱ Timeout after {entry.payload.get('duration', '?')}s"
            if error == "subprocess_failed":
                return (
                    f"💥 Subprocess failed (rc={entry.payload.get('return_code', '?')})"
                )
            if error == "malformed_output":
                stage = entry.payload.get("stage", "")
                return f"⚠ Malformed JSON (stage={stage})"
            return f"Error: {error}"
        summary = entry.payload.get("summary", "")[:60]
        if entry.payload.get("status") == "blocked":
            blocker = entry.payload.get("blocker_description", "")[:50]
            return f"🚫 Blocked: {blocker}" if blocker else summary
        return summary
    if entry.actor == Actor.QA:
        decision = entry.payload.get("decision", "")[:60]
        feedback = entry.payload.get("feedback", "")[:40]
        if decision:
            prefix = {"approve": "✓", "reject": "✗", "needs_user_input": "❓"}.get(
                decision, ""
            )
            suffix = f" — {feedback}" if feedback else ""
            return f"{prefix} {decision}{suffix}"
        return feedback
    return ""
