"""tasker — Goose-based task orchestration CLI with QA/Dev feedback loop.

Usage:
    uv run python -m tasker --dev recipe-dev.yaml --qa recipe-qa.yaml specs/arch/99-todo.md
    uv run python -m tasker --dev recipe-dev.yaml --qa recipe-qa.yaml specs/arch/99-todo.md --start-phase 3
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

from .monitoring import setup_monitoring
from .orchestrator import Orchestrator
from .models import (
    CLAUDE_CODE_DEFAULT_MODEL,
    CLAUDE_CODE_PROVIDER,
    ESCALATION_DEFAULT_TIMEOUT_SECS,
    Actor,
    EscalationConfig,
    FallbackModel,
    RateLimitConfig,
    SessionScope,
)

# Default recipes shipped with tasker, resolved relative to this file.
_RECIPES_DIR = Path(__file__).resolve().parent.parent.parent / "recipes"
_DEFAULT_DEV = _RECIPES_DIR / "recipe-dev.yaml"
_DEFAULT_QA = _RECIPES_DIR / "recipe-qa.yaml"
_DEFAULT_ARCH = _RECIPES_DIR / "recipe-arch.yaml"

app = typer.Typer(
    name="tasker",
    help="Orchestrate goose-based QA/Dev feedback loops from a markdown task list.",
    no_args_is_help=True,
)
console = Console()

# Register the ``tasker prepare`` subcommand group.
from .prepare import prepare_app  # noqa: E402

app.add_typer(prepare_app, name="prepare")


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.exists():
        console.print(f"[bold red]Error:[/bold red] File not found: {p}")
        raise typer.Exit(1)
    return p


class FlagPairError(ValueError):
    """A provider/model CLI flag pair was only half given."""


def _check_pair(flag: str, provider: str | None, model: str | None) -> None:
    """Reject ``--{flag}-provider`` without ``--{flag}-model`` or vice versa."""
    if bool(provider) != bool(model):
        raise FlagPairError(
            f"--{flag}-provider and --{flag}-model must be given together "
            f"(got provider={provider!r}, model={model!r})."
        )


def _resolve_fallback(
    *,
    explicit_provider: str | None,
    explicit_model: str | None,
    primary_provider: str | None,
    no_auto_fallback: bool,
    auto_fallback_model: str | None,
    role: str,
    timeout_secs: int | None = None,
) -> FallbackModel | None:
    """Resolve a per-role fallback model.

    Precedence (highest → lowest):

    1. **Explicit CLI flags** — ``--fallback-{role}-provider`` together with
       ``--fallback-{role}-model``.  Giving only one of the two is an error
       (:class:`FlagPairError`).
    2. **Automatic claude-code fallback** — when the ``claude`` CLI is on
       ``PATH`` (and the user has not passed ``--no-auto-fallback``), a
       :class:`FallbackModel` with ``provider="claude-code"`` is synthesised.
       It runs on the Claude subscription, no API key needed.  The model
       defaults to ``sonnet`` and can be overridden via
       ``--auto-fallback-model``.
    3. **None** — no fallback.

    The automatic fallback is **skipped** when the primary provider is
    itself ``claude-code`` — falling back to the same provider is pointless.

    *timeout_secs* (``--fallback-timeout``) applies to either fallback;
    ``None`` keeps the primary call's ``--timeout``.
    """
    _check_pair(f"fallback-{role}", explicit_provider, explicit_model)

    # 1. Explicit CLI flags take precedence.
    if explicit_provider and explicit_model:
        return FallbackModel(
            provider=explicit_provider, model=explicit_model, timeout_secs=timeout_secs
        )

    # 2. Auto-configure the claude-code fallback when the CLI is installed.
    if not no_auto_fallback and primary_provider != CLAUDE_CODE_PROVIDER:
        if shutil.which("claude"):
            model = auto_fallback_model or CLAUDE_CODE_DEFAULT_MODEL
            return FallbackModel(
                provider=CLAUDE_CODE_PROVIDER, model=model, timeout_secs=timeout_secs
            )

    # 3. No fallback.
    return None


def _resolve_escalation(
    *,
    enabled: bool,
    provider: str | None,
    model: str | None,
    roles: str,
    timeout_secs: int = ESCALATION_DEFAULT_TIMEOUT_SECS,
) -> EscalationConfig | None:
    """Build the escalation config for stuck tasks, or None when disabled.

    Escalation is on when ``--escalate`` is passed or when either
    ``--escalate-provider`` / ``--escalate-model`` is given; the missing
    half defaults to ``claude-code`` / ``sonnet``.  *roles* is a
    comma-separated subset of ``arch,dev,qa``.  Escalated calls use
    *timeout_secs* (``--escalate-timeout``) instead of ``--timeout``.
    """
    if not (enabled or provider or model):
        return None
    valid = {Actor.ARCH.value, Actor.DEV.value, Actor.QA.value}
    names = {r.strip().lower() for r in roles.split(",") if r.strip()}
    if not names or not names <= valid:
        raise ValueError(
            f"Invalid --escalate-roles {roles!r}: "
            f"use a comma-separated subset of {', '.join(sorted(valid))}."
        )
    return EscalationConfig(
        provider=provider or CLAUDE_CODE_PROVIDER,
        model=model or CLAUDE_CODE_DEFAULT_MODEL,
        roles=frozenset(Actor(n) for n in names),
        timeout_secs=timeout_secs,
    )


def _warn_goose_mode(*models: FallbackModel | EscalationConfig | None) -> None:
    """Warn when claude-code is configured but GOOSE_MODE is not ``auto``.

    goose's approve / smart_approve modes refuse every tool call of the
    headless claude-code provider.  tasker does not change the approval
    mode itself; set ``GOOSE_MODE=auto`` (environment or goose config).
    """
    uses_claude_code = any(
        m is not None and m.provider == CLAUDE_CODE_PROVIDER for m in models
    )
    if uses_claude_code and os.environ.get("GOOSE_MODE", "").lower() != "auto":
        console.print(
            "[yellow]Warning:[/yellow] the claude-code fallback/escalation needs "
            "GOOSE_MODE=auto; with approve/smart_approve goose rejects its tool "
            "calls. Export GOOSE_MODE=auto (or set it in the goose config)."
        )


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
    decompose: Path = typer.Option(
        None,
        "--decompose",
        help="Path to a task decomposition recipe. When set, QA reviews each task before passing to DEV and may split it into subtasks.",
    ),
    arch: Path = typer.Option(
        _DEFAULT_ARCH,
        "--arch",
        help="Path to the Architect goose recipe (YAML). Invoked when a task gets stuck. Default: recipe-arch.yaml.",
    ),
    no_arch: bool = typer.Option(
        False,
        "--no-arch",
        help="Disable the Architect agent — stuck tasks will just be marked blocked.",
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
        "task",
        "--session-scope",
        help="When to rotate goose sessions: phase (per ## heading), subphase (per ### heading), or task (per task, default).",
    ),
    new_session: bool = typer.Option(
        False,
        "--new-session",
        help="Force creation of a new goose session on the next task (one-shot).",
    ),
    rate_limit_base_delay: float = typer.Option(
        30.0,
        "--rate-limit-base-delay",
        help="Base delay in seconds for exponential backoff on connection errors. Default: 30.",
    ),
    rate_limit_max_delay: float = typer.Option(
        300.0,
        "--rate-limit-max-delay",
        help="Maximum backoff delay in seconds for connection errors. Default: 300.",
    ),
    rate_limit_max_retries: int = typer.Option(
        5,
        "--rate-limit-max-retries",
        help="Max retries on transient connection/rate-limit errors. Default: 5.",
    ),
    rate_limit_disabled: bool = typer.Option(
        False,
        "--no-rate-limit",
        help="Disable automatic backoff on connection errors entirely.",
    ),
    max_consecutive_empty: int = typer.Option(
        3,
        "--max-consecutive-empty",
        help="Max consecutive empty-output goose calls before marking a task as permanently failed ([~]). Default: 3.",
    ),
    fallback_dev_model: str | None = typer.Option(
        None,
        "--fallback-dev-model",
        help="Fallback model for dev role when primary is unreachable (e.g. qwen3.5:9b). Requires --fallback-dev-provider.",
    ),
    fallback_dev_provider: str | None = typer.Option(
        None,
        "--fallback-dev-provider",
        help="Fallback provider for dev role (e.g. ollama). Requires --fallback-dev-model.",
    ),
    fallback_qa_model: str | None = typer.Option(
        None,
        "--fallback-qa-model",
        help="Fallback model for QA role when primary is unreachable (e.g. claude-sonnet-4). Requires --fallback-qa-provider.",
    ),
    fallback_qa_provider: str | None = typer.Option(
        None,
        "--fallback-qa-provider",
        help="Fallback provider for QA role (e.g. anthropic). Requires --fallback-qa-model.",
    ),
    no_auto_fallback: bool = typer.Option(
        False,
        "--no-auto-fallback",
        "--no-anthropic-fallback",
        help="Disable the automatic claude-code fallback (used when the `claude` CLI is on PATH).",
    ),
    auto_fallback_model: str | None = typer.Option(
        None,
        "--auto-fallback-model",
        "--anthropic-fallback-model",
        help="Model for the automatic claude-code fallback. Default: sonnet.",
    ),
    escalate: bool = typer.Option(
        False,
        "--escalate",
        help=(
            "When a task is flagged as stuck, switch its ARCH/DEV calls (see "
            "--escalate-roles) to a stronger model until the task changes. "
            "Default model: claude-code/sonnet."
        ),
    ),
    escalate_provider: str | None = typer.Option(
        None,
        "--escalate-provider",
        help="Provider for stuck-task escalation (implies --escalate). Default: claude-code.",
    ),
    escalate_model: str | None = typer.Option(
        None,
        "--escalate-model",
        help="Model for stuck-task escalation (implies --escalate), e.g. opus. Default: sonnet.",
    ),
    escalate_roles: str = typer.Option(
        "arch,dev",
        "--escalate-roles",
        help="Comma-separated roles that run escalated: subset of arch,dev,qa. Default: arch,dev.",
    ),
    escalate_timeout: int = typer.Option(
        ESCALATION_DEFAULT_TIMEOUT_SECS,
        "--escalate-timeout",
        min=1,
        help="Timeout in seconds for each escalated goose call (replaces --timeout). Default: 1800.",
    ),
    fallback_timeout: int | None = typer.Option(
        None,
        "--fallback-timeout",
        min=1,
        help="Timeout in seconds for each fallback goose call. Default: same as --timeout.",
    ),
    monitor_log: Path = typer.Option(
        None,
        "--monitor-log",
        help=(
            "Path for the structured monitor log file (default: tasker.log in the "
            "task file's parent directory). Captures orchestration decisions, "
            "recovery escalations, session rotations, VCS ops, subprocess launches, "
            "and parser events. Use --no-monitor-log to disable file logging."
        ),
    ),
    no_monitor_log: bool = typer.Option(
        False,
        "--no-monitor-log",
        help="Disable the monitor log file (only console output).",
    ),
    cwd: Path | None = typer.Option(
        None,
        "--cwd",
        help="Project root for goose agents. Default: current working directory (where you run tasker from).",
    ),
    log_level: str = typer.Option(
        "WARNING",
        "--log-level",
        help="Minimum log level for console (stderr) output. One of: debug, info, warning, error, critical. Default: WARNING (quiet terminal).",
    ),
    file_log_level: str = typer.Option(
        "DEBUG",
        "--file-log-level",
        help="Minimum log level for the monitor log file. One of: debug, info, warning, error, critical. Default: DEBUG (capture everything).",
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

    # Use --cwd if provided, otherwise default to the current working directory
    # (where the user invoked tasker from).  This ensures goose agents can
    # resolve project-relative paths like specs/... and crates/... correctly.
    cwd = Path(cwd).resolve() if cwd else Path(os.getcwd()).resolve()

    # ── Configure structured monitoring log ──────────────────────
    if no_monitor_log:
        monitor_log_path = None
    elif monitor_log:
        monitor_log_path = monitor_log
    else:
        monitor_log_path = cwd / "tasker.log"

    try:
        setup_monitoring(
            monitor_log_path, console_level=log_level, file_level=file_log_level
        )
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)

    # Resolve recipe paths to absolute so goose can find them regardless of cwd
    dev_abs = dev.resolve()
    qa_abs = qa.resolve()

    # Resolve ARCH recipe (default: recipe-arch.yaml, disabled with --no-arch)
    if no_arch:
        arch_abs = None
    else:
        if not arch.exists():
            console.print(
                f"[bold red]Error:[/bold red] Architect recipe not found: {arch}\n"
                f"  Use --arch to specify an alternate path, or --no-arch to disable."
            )
            raise typer.Exit(1)
        arch_abs = arch.resolve()

    # Resolve fallback / escalation models (validates the flag pairs)
    try:
        fallback_dev = _resolve_fallback(
            explicit_provider=fallback_dev_provider,
            explicit_model=fallback_dev_model,
            primary_provider=provider,
            no_auto_fallback=no_auto_fallback,
            auto_fallback_model=auto_fallback_model,
            role="dev",
            timeout_secs=fallback_timeout,
        )
        fallback_qa = _resolve_fallback(
            explicit_provider=fallback_qa_provider,
            explicit_model=fallback_qa_model,
            primary_provider=provider,
            no_auto_fallback=no_auto_fallback,
            auto_fallback_model=auto_fallback_model,
            role="qa",
            timeout_secs=fallback_timeout,
        )
        escalation = _resolve_escalation(
            enabled=escalate,
            provider=escalate_provider,
            model=escalate_model,
            roles=escalate_roles,
            timeout_secs=escalate_timeout,
        )
    except ValueError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1)
    _warn_goose_mode(fallback_dev, fallback_qa, escalation)

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
        rate_limit=RateLimitConfig(
            enabled=not rate_limit_disabled,
            base_delay_secs=rate_limit_base_delay,
            max_delay_secs=rate_limit_max_delay,
            max_retries=rate_limit_max_retries,
            fallback_dev=fallback_dev,
            fallback_qa=fallback_qa,
        ),
        decompose_recipe=str(decompose.resolve()) if decompose else None,
        arch_recipe=arch_abs,
        max_consecutive_empty=max_consecutive_empty,
        escalation=escalation,
    )

    orchestrator.run()


if __name__ == "__main__":
    app()
