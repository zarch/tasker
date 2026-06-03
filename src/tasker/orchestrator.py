"""QA↔Dev orchestrator — the core feedback loop."""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .goose import GooseRunResult, run_goose_with_backoff
from .log import IterationLog
from .mem import MemorySnapshot, delta, format_snapshot_human, snapshot
from .models import (
    Actor,
    ArchAction,
    ArchRequest,
    ArchResponse,
    DecomposeResponse,
    DevRequest,
    DevResponse,
    FallbackModel,
    IterationEntry,
    Phase,
    QARecoveryStage,
    QAResponse,
    QARequest,
    RecoveryStage,
    SessionScope,
    Subtask,
    Task,
    TaskStatus,
    RateLimitConfig,
    UserChatRequest,
)
from .schema import ArchResponse as ArchResponseSchema
from .schema import DecomposeResponse as DecomposeResponseSchema
from .schema import DevResponse as DevResponseSchema
from .schema import QAResponse as QAResponseSchema
from .adapter import (
    insert_subtasks_dispatch,
    load_tasks,
    mark_done,
    rewrite_text,
)
from .parser import (
    find_next_task,
    mark_task_failed,
)
from .ui import TaskerUI
from .vcs import VCSBackend


log = structlog.get_logger(__name__)


def _generate_session_id(prefix: str) -> str:
    """Create a persistent, human-readable session ID."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    uid = uuid.uuid4().hex[:6]
    return f"{prefix}_{ts}_{uid}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_dev_response(raw: str, parsed: dict | None) -> DevResponse | None:
    """Extract DevResponse from goose output. Returns None if unparsable."""
    if parsed and "status" in parsed:
        # Primary path: Pydantic validation
        try:
            validated = DevResponseSchema.model_validate(parsed)
            return DevResponse(
                status=validated.status,
                summary=validated.summary,
                files_modified=validated.files_modified,
                notes=validated.notes,
                blocker_description=validated.blocker_description,
                blocker_suggestion=validated.blocker_suggestion,
            )
        except Exception:
            # Fallback: ad-hoc dict extraction for edge cases Pydantic rejects
            status = parsed["status"]
            if status not in ("done", "blocked", "started"):
                return None  # unknown status — treat as malformed
            return DevResponse(
                status=status,
                summary=parsed.get("summary", ""),
                files_modified=parsed.get("files_modified", []),
                notes=parsed.get("notes", ""),
                blocker_description=parsed.get("blocker_description", ""),
                blocker_suggestion=parsed.get("blocker_suggestion", ""),
            )
    return None


def _parse_qa_response(raw: str, parsed: dict | None) -> QAResponse | None:
    """Extract QAResponse from goose output. Returns None if unparsable."""
    if parsed and "decision" in parsed:
        # Primary path: Pydantic validation
        try:
            validated = QAResponseSchema.model_validate(parsed)
            return QAResponse(
                decision=validated.decision,
                feedback=validated.feedback,
                concerns=validated.concerns,
                user_question=validated.user_question,
            )
        except Exception:
            # Fallback: ad-hoc dict extraction for edge cases Pydantic rejects
            decision = parsed["decision"]
            if decision not in ("approve", "reject", "needs_user_input"):
                return None  # unknown decision — treat as malformed
            return QAResponse(
                decision=decision,
                feedback=parsed.get("feedback", ""),
                concerns=parsed.get("concerns", []),
                user_question=parsed.get("user_question", ""),
            )
    return None


def _parse_decompose_response(
    raw: str, parsed: dict | None
) -> DecomposeResponse | None:
    """Extract DecomposeResponse from goose output. Returns None if unparsable."""
    if parsed and "should_decompose" in parsed:
        # Primary path: Pydantic validation
        try:
            validated = DecomposeResponseSchema.model_validate(parsed)
            return DecomposeResponse(
                should_decompose=validated.should_decompose,
                reason=validated.reason,
                subtasks=[
                    Subtask(label=st.label, text=st.text) for st in validated.subtasks
                ],
            )
        except Exception:
            # Fallback: ad-hoc dict extraction for edge cases Pydantic rejects
            # (e.g. should_decompose as string "true"/"false" instead of bool)
            should = parsed["should_decompose"]
            if not isinstance(should, bool):
                should = str(should).lower() == "true"
            subtasks: list[Subtask] = []
            for st in parsed.get("subtasks", []):
                if isinstance(st, dict) and st.get("label") and st.get("text"):
                    subtasks.append(Subtask(label=st["label"], text=st["text"]))
            return DecomposeResponse(
                should_decompose=should,
                reason=parsed.get("reason", ""),
                subtasks=subtasks,
            )
    return None


def _parse_arch_response(raw: str, parsed: dict | None) -> ArchResponse | None:
    """Extract ArchResponse from goose output. Returns None if unparsable."""
    if parsed and "action" in parsed:
        # Primary path: Pydantic validation
        try:
            validated = ArchResponseSchema.model_validate(parsed)
            return ArchResponse(
                action=validated.action,
                reason=validated.reason,
                subtasks=[
                    Subtask(label=st.label, text=st.text) for st in validated.subtasks
                ],
                new_task_text=validated.new_task_text,
                max_iterations_override=validated.max_iterations_override,
            )
        except Exception:
            # Fallback: ad-hoc dict extraction for edge cases Pydantic rejects
            action = parsed["action"]
            if action not in ("recompose", "clarify", "skip", "retry"):
                return None
            subtasks: list[Subtask] = []
            for st in parsed.get("subtasks", []):
                if isinstance(st, dict) and st.get("label") and st.get("text"):
                    subtasks.append(Subtask(label=st["label"], text=st["text"]))
            return ArchResponse(
                action=action,
                reason=parsed.get("reason", ""),
                subtasks=subtasks,
                new_task_text=parsed.get("new_task_text", ""),
                max_iterations_override=parsed.get("max_iterations_override"),
            )
    return None


# ── Recovery instructions for graceful degradation ────────────────

_TASK_CONTEXT_TEMPLATE = (
    "## YOUR TASK (repeated for recovery):\n**{task_label}**: {task_text}\n\n"
)

_RECOVERY_CONTINUE = (
    "⚠️ FORMAT RECOVERY — your previous response had no JSON block.\n\n"
    "CRITICAL RULES — follow in order:\n"
    "1. DO NOT read any files, specs, or explore the codebase.\n"
    "2. DO NOT start over or re-investigate anything.\n"
    "3. Based on what you already know, output the JSON block immediately:\n"
    '{"status": "done"|"blocked", "summary": "...", "files_modified": [...], ...}'
)

_RECOVERY_SUBTASK = (
    "⚠️ FORMAT RECOVERY — your last responses had no JSON block.\n\n"
    "CRITICAL RULES — follow in order:\n"
    "1. DO NOT read any files, specs, or explore the codebase.\n"
    "2. Pick the SINGLE most important piece from the task description.\n"
    "3. Write ONLY the minimal skeleton (struct/fn stubs with TODO bodies). Max 20 lines.\n"
    "4. Output the JSON block IMMEDIATELY after the skeleton:\n"
    '{"status": "done", "summary": "Wrote skeleton: <file>", "files_modified": [...]}\n\n'
    "CRITICAL: Do NOT claim the task is already done unless you have personally written or "
    'verified the code in THIS session. If no code changes were made, report status "blocked" '
    'with a description of what prevented progress, NOT status "done".'
)

_RECOVERY_TRUNCATION = (
    "⚠️ OUTPUT TRUNCATED — your previous response was cut off.\n\n"
    "CRITICAL RULES — you MUST follow these in order:\n"
    "1. DO NOT read any files, specs, or explore the codebase. You already have context.\n"
    "2. DO NOT write more than 30 lines of code in a single tool call.\n"
    "3. Write a MINIMAL skeleton (struct/enum/fn signatures with TODO bodies only).\n"
    "4. Immediately after the skeleton, output the JSON block below.\n\n"
    '{"status": "done", "summary": "Wrote skeleton: <file>", "files_modified": [...]}'
)

_RECOVERY_SUMMARIZE = (
    "⚠️ FORMAT RECOVERY — stop implementing immediately.\n\n"
    "Output ONLY this JSON block with your progress:\n"
    '{"status": "blocked", "summary": "<what you did so far>", '
    '"files_modified": [...], "blocker_description": "<what remains>"}\n\n'
    "CRITICAL: Do NOT claim the task is already done unless you have personally written or "
    'verified the code in THIS session. If no code changes were made, report status "blocked" '
    'with a description of what prevented progress, NOT status "done".'
)

_RECOVERY_RESTART = (
    "⚠️ FRESH START — previous attempts produced no JSON block.\n\n"
    "CRITICAL RULES — follow in order:\n"
    "1. DO NOT read any files, specs, or explore the codebase.\n"
    "2. Based ONLY on the task description provided, create the target file(s) as minimal stubs.\n"
    "3. Write empty function bodies / TODO placeholders — correctness not required.\n"
    "4. Output the JSON block immediately after writing:\n"
    '{"status": "done", "summary": "Created stubs for: <file>", "files_modified": [...]}\n\n'
    "CRITICAL: Do NOT claim the task is already done unless you have personally written or "
    'verified the code in THIS session. If no code changes were made, report status "blocked" '
    'with a description of what prevented progress, NOT status "done".'
)


def _with_task_context(instruction: str, task_label: str, task_text: str) -> str:
    """Prepend the task description to a recovery instruction.

    This ensures the agent always has its task available even when
    recovery mode suppresses the normal task_text parameter or when
    context compaction has purged the original task from the session.
    """
    ctx = _TASK_CONTEXT_TEMPLATE.format(task_label=task_label, task_text=task_text)
    return ctx + instruction


# ── QA recovery instructions for graceful degradation ─────────────

_QA_RECOVERY_CONTINUE = (
    "⚠️ FORMAT RECOVERY — your last response had no JSON decision block.\n\n"
    "You already completed the review. DO NOT re-read files or re-investigate. "
    "Output ONLY the JSON decision block now:\n"
    '{"decision": "approve"|"reject"|"needs_user_input", "feedback": "...", '
    '"concerns": [...]}'
)

_QA_RECOVERY_SUMMARIZE = (
    "⚠️ FORMAT RECOVERY — stop reading files immediately.\n\n"
    "Based on what you already know, output ONLY the JSON decision block:\n"
    '{"decision": "approve"|"reject"|"needs_user_input", "feedback": "...", '
    '"concerns": [...]}\n\n'
    "CRITICAL: Base your decision only on code you can verify exists. Do not approve based "
    "on the developer's claim that work was done previously — check the actual file changes."
)

_QA_RECOVERY_RESTART = (
    "⚠️ FRESH START — previous review attempts produced no JSON block.\n\n"
    "You are in a new session. Review concisely and output the JSON block.\n\n"
    "CRITICAL: Base your decision only on code you can verify exists. Do not approve based "
    "on the developer's claim that work was done previously — check the actual file changes."
)

# ── Stuckness detection constants ──────────────────────────────────
# Consecutive full-recovery exhaustions before invoking ARCH
_STUCK_EXHAUSTION_THRESHOLD = 2
# Max feedback-loop iterations without QA approval before invoking ARCH
_STUCK_ITERATION_THRESHOLD = 10
# Maximum feedback text length (chars) — prevents ARG_MAX overflow
_MAX_FEEDBACK_LENGTH = 2000
# Keep only the last N feedback rounds when truncating
_MAX_FEEDBACK_ROUNDS = 3


_TRUNCATION_MARKER = "A tool call could not be parsed"


def _is_truncated_output(raw_stdout: str | None) -> bool:
    """Detect goose truncation errors in agent output.

    When the LLM output exceeds the model max tokens, goose truncates the
    response mid-tool-call and inserts this error message. The agent then
    cannot produce a JSON response because the file write never completed.
    """
    return _TRUNCATION_MARKER in raw_stdout if raw_stdout else False


def _is_checkpoint(response: DevResponse | QAResponse) -> bool:
    """Check if a response signals a checkpoint pause.

    For DevResponse: status="started" (primary), or status="blocked" with
    'checkpoint' in notes (legacy fallback).
    For QAResponse: rejected with 'checkpoint' in feedback.
    """
    if isinstance(response, DevResponse):
        return response.status == "started" or (
            response.status == "blocked" and "checkpoint" in response.notes
        )
    elif isinstance(response, QAResponse):
        return response.decision == "reject" and "checkpoint" in response.feedback
    return False


def _timeout_feedback(actor: str, timeout_secs: int) -> str:
    """Build a feedback message to send when an agent was killed for timing out."""
    timeout_minutes = timeout_secs / 60
    return (
        f"## ⚠️ {actor} Process Killed — Timeout\n\n"
        f"Your previous run was **killed** because it was stuck for more than "
        f"{timeout_minutes:.0f} minutes ({timeout_secs} seconds) without completing.\n\n"
        f"**What happened:** The process was running for too long and was "
        f"automatically terminated.\n\n"
        f"**What to do now:**\n"
        f"1. Review where you left off in your previous session (you still have context).\n"
        f"2. Finish the task as quickly and efficiently as possible.\n"
        f"3. You MUST end your response with the required JSON block.\n\n"
        f"Do NOT redo work you have already completed. Continue from where you "
        f"were interrupted and wrap up promptly.\n"
    )


# Matches paths in backticks containing known source directories
_DIR_REF_RE = re.compile(r"`([^`\s]+/(?:src|tests|crates|modules|pkg)/[^`\s]*)`")


def _extract_dir_refs(text: str, cwd: Path) -> set[Path]:
    """Extract directory references from task text.

    Looks for paths in backticks that contain known source directories
    (src/, tests/, crates/, modules/, pkg/).  Returns the set of
    unique root directories (the top-level dir containing src/ or tests/).

    For example:
      `eudox-mcp/src/eudox_mcp/server.py` → {cwd / "eudox-mcp"}
      `plans-mcp/tests/test_auth.py`       → {cwd / "plans-mcp"}
      `src/eudox/pipeline/analytics.py`    → {} (inside cwd repo, no sub-dir)
    """
    roots: set[Path] = set()
    for m in _DIR_REF_RE.finditer(text):
        raw_path = m.group(1)
        parts = Path(raw_path).parts
        # Find the boundary: the dir that directly contains src/ or tests/
        for i, part in enumerate(parts):
            if part in ("src", "tests", "crates", "modules", "pkg") and i > 0:
                root = cwd / Path(*parts[:i])
                if root.is_dir():
                    roots.add(root)
                break
    return roots


def _compute_scope_key(task: Task, scope: SessionScope) -> str:
    """Compute a scope key for a task based on the session scope setting.

    phase    → "P1"
    subphase → "P1::P1-1 Core" (or "P1" if no subphase)
    task     → "P1::P1-1 Core::T3" (or "P1::T3" if no subphase)
    """
    phase_key = f"P{task.phase_index + 1}"
    if scope == SessionScope.PHASE:
        return phase_key
    if scope == SessionScope.SUBPHASE:
        return f"{phase_key}::{task.subphase}" if task.subphase else phase_key
    # TASK — use subphase-local index when available for meaningful keys
    if task.subphase and task.subphase_index >= 0:
        return f"{phase_key}::{task.subphase}::T{task.subphase_index + 1}"
    return f"{phase_key}::T{task.task_index + 1}"


class Orchestrator:
    """Drives the QA → Dev → QA loop for all tasks in the task file."""

    def __init__(
        self,
        task_file: str | Path,
        dev_recipe: str | Path,
        qa_recipe: str | Path,
        log_file: str | Path,
        max_iterations_per_task: int = 10,
        max_turns: int = 80,
        timeout_secs: int = 600,
        model: str | None = None,
        provider: str | None = None,
        cwd: str | Path | None = None,
        start_phase: int | None = None,
        vcs: VCSBackend | None = None,
        session_scope: SessionScope = SessionScope.SUBPHASE,
        force_new_session: bool = False,
        rate_limit: RateLimitConfig | None = None,
        decompose_recipe: str | Path | None = None,
        arch_recipe: str | Path | None = None,
        max_consecutive_empty: int = 3,
    ) -> None:
        self.task_file = Path(task_file).resolve()
        self.dev_recipe = Path(dev_recipe)
        self.qa_recipe = Path(qa_recipe)
        self.decompose_recipe = Path(decompose_recipe) if decompose_recipe else None
        self.arch_recipe = Path(arch_recipe) if arch_recipe else None
        self.log = IterationLog(log_file)
        self.ui = TaskerUI()
        self.max_iterations = max_iterations_per_task
        self.max_turns = max_turns
        self.timeout_secs = timeout_secs
        self.model = model
        self.provider = provider
        self.cwd = Path(cwd) if cwd else None
        self.start_phase = start_phase

        # VCS integration (jj or git backend, or None)
        self.vcs = vcs
        # Multi-repo mode: when CWD isn't a git repo but subdirs are,
        # we track them here and commit per-repo on task approval.
        self._git_repos: list[Path] = []

        # Session scope — controls when new goose sessions are created
        self.session_scope = session_scope
        self._current_scope_key: str = ""  # tracks the current scope boundary
        self._force_new_session = force_new_session  # one-shot flag

        # Rate-limit / connection-error backoff
        self.rate_limit = rate_limit or RateLimitConfig()

        # Circuit breaker: max consecutive empty-output goose calls before
        # skipping a task entirely (prevents the endless malformed_output loop).
        self.max_consecutive_empty = max_consecutive_empty

        # goose run uses --name for session persistence and auto-resumes
        # when the same name is used again.
        self.dev_session_name = _generate_session_id("dev")
        self.qa_session_name = _generate_session_id("qa")

        # State
        self.phases: list[Phase] = []
        self.current_phase: Phase | None = None
        self.global_iteration = 0

        # Stuckness tracking — per-task counters reset when task changes
        self._stuck_task_label: str = ""
        self._consecutive_exhaustions: int = 0
        self._iterations_without_approval: int = 0
        self._pending_arch_check: bool = False

        # Memory tracking — snapshots around goose subprocesses
        self._last_memory_snapshot: MemorySnapshot | None = None

    def run(self) -> None:
        """Main entry point — run all tasks."""
        log.info(
            "orchestrator.starting",
            dev_session=self.dev_session_name,
            qa_session=self.qa_session_name,
            session_scope=self.session_scope.value,
            iteration_log=str(self.log._path),
            task_file=str(self.task_file),
            max_iterations=self.max_iterations,
            max_turns=self.max_turns,
            timeout_secs=self.timeout_secs,
            model=self.model,
            provider=self.provider,
            cwd=str(self.cwd),
            vcs="enabled" if self.vcs else "disabled",
            arch="enabled" if self.arch_recipe else "disabled",
        )

        # Log initial memory state
        initial_snap = snapshot()
        if initial_snap is not None:
            self._last_memory_snapshot = initial_snap
            log.info("memory.initial", **initial_snap.to_dict())
            self.ui.print_info(f"Memory: {format_snapshot_human(initial_snap)}")

        self.ui.print_info(f"Developer session: {self.dev_session_name}")
        self.ui.print_info(f"QA session:       {self.qa_session_name}")
        self.ui.print_info(f"Session scope:    {self.session_scope.value}")
        self.ui.print_info(f"Iteration log:    {self.log._path}")
        if self.arch_recipe:
            self.ui.print_info(f"Architect recipe: {self.arch_recipe}")
        self.ui.print_info("")

        # Initialize VCS integration
        if self.vcs is not None:
            if not self.vcs.is_available():
                log.warning("vcs.disabled", reason="tool_not_found_in_path")
                self.ui.print_error(
                    "VCS tool not found in PATH. Disabling VCS integration."
                )
                self.vcs = None
            else:
                try:
                    self.vcs.init(cwd=self.cwd)
                    log.info("vcs.initialized", cwd=str(self.cwd))
                    self.ui.print_info("VCS integration: ON")
                except RuntimeError as exc:
                    log.error("vcs.init_failed", error=str(exc))
                    # Single-repo init failed — try multi-repo discovery.
                    # This handles the common case where the CWD is a parent
                    # directory containing multiple git repos as subdirectories.
                    self._git_repos = self._discover_git_repos()
                    if self._git_repos:
                        self.vcs = None  # disable single-repo backend
                        repo_names = ", ".join(p.name for p in self._git_repos)
                        log.info(
                            "vcs.multi_repo",
                            repos=repo_names,
                            count=len(self._git_repos),
                        )
                        self.ui.print_info(
                            f"VCS integration: multi-repo mode "
                            f"({len(self._git_repos)} repos: {repo_names})"
                        )
                    else:
                        self.ui.print_error(f"VCS init failed: {exc}")
                        self.vcs = None

        # Parse tasks
        self.phases = load_tasks(self.task_file)

        if not self.phases:
            log.error("parser.no_phases", task_file=str(self.task_file))
            self.ui.print_error("No phases found in task file. Nothing to do.")
            return

        # If start_phase is specified, mark all earlier tasks as done
        if self.start_phase is not None:
            skipped = 0
            for phase in self.phases:
                if phase.index < self.start_phase:
                    for task in phase.tasks:
                        task.done = True
                        skipped += 1
            log.info(
                "start_phase.skipped",
                start_phase=self.start_phase,
                tasks_skipped=skipped,
            )

        # Reset [~] (permanently failed) tasks back to [ ] (pending).
        # This gives tasks a fresh chance on each new orchestrator run —
        # the previous failure was likely due to a transient issue (e.g.
        # the checkpoint feedback loop bug) rather than an intrinsic problem.
        reset_count = 0
        for phase in self.phases:
            for task in phase.tasks:
                if task.failed:
                    task.failed = False
                    task.skipped = False
                    reset_count += 1
        if reset_count > 0:
            from .parser import update_markdown

            update_markdown(self.task_file, self.phases)
            log.info("tasks.reset_failed", count=reset_count)
            self.ui.print_info(
                f"♻️  Reset {reset_count} previously failed task(s) — giving them a fresh attempt"
            )

        total_tasks = sum(p.total for p in self.phases)
        done_tasks = sum(p.completed for p in self.phases)
        remaining = total_tasks - done_tasks
        log.info(
            "tasks.loaded",
            phases=len(self.phases),
            total_tasks=total_tasks,
            done_tasks=done_tasks,
            remaining=remaining,
        )
        self.ui.print_info(
            f"Loaded {len(self.phases)} phases, {total_tasks} tasks "
            f"({done_tasks} already done, {remaining} remaining)"
        )
        self.ui.print_info("")

        # Log session-start entry to the JSONL iteration log.
        # This makes it easy to find where a new orchestrator invocation
        # begins when inspecting logs from multiple runs.
        from datetime import datetime, timezone

        session_start_entry = IterationEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            iteration=0,
            actor=Actor.SYSTEM,
            task_label="—",
            status=TaskStatus.SESSION_START,
            payload={
                "dev_session": self.dev_session_name,
                "qa_session": self.qa_session_name,
                "session_scope": self.session_scope.value,
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
                "remaining": remaining,
                "failed_reset": reset_count,
                "task_file": str(self.task_file),
                "model": self.model or "",
                "provider": self.provider or "",
                "max_iterations": self.max_iterations,
                "max_turns": self.max_turns,
            },
        )
        self.log.append(session_start_entry)
        log.info(
            "session.start",
            dev_session=self.dev_session_name,
            qa_session=self.qa_session_name,
            failed_reset=reset_count,
        )

        # Auto-init VCS in subdirectories referenced by tasks
        if self.vcs is not None:
            assert self.cwd is not None  # VCS requires a working directory
            untracked = self._scan_vcs_paths()
            for path in untracked:
                try:
                    rel = path.relative_to(self.cwd)
                except ValueError:
                    rel = path
                self.ui.print_info(f"VCS: auto-init git in {rel}")
                try:
                    self.vcs.init_subdir(path)
                except RuntimeError as exc:
                    log.warning("vcs.auto_init_failed", path=str(path), error=str(exc))
                    self.ui.print_warning(
                        f"VCS: failed to auto-init {path}: {exc}. "
                        f"Files in this directory will not be VCS-tracked."
                    )

        # Start live UI
        self.ui.init_progress()
        self.ui.start()
        self.ui.update_project(self.phases, self.phases[0])

        try:
            self._run_loop()
        finally:
            self.ui.stop()

        # Final summary
        total_tasks = sum(p.total for p in self.phases)
        done_tasks = sum(p.completed for p in self.phases)

        # Final memory summary
        final_snap = snapshot()
        if final_snap is not None:
            final_dict = final_snap.to_dict()
            log.info("memory.final", **final_dict)
            if self._last_memory_snapshot is not None:
                mem_delta = delta(self._last_memory_snapshot, final_snap)
                log.info("memory.session_delta", **mem_delta)
            self.ui.print_info(f"Final memory: {format_snapshot_human(final_snap)}")

        log.info(
            "orchestrator.finished",
            total_tasks=total_tasks,
            completed=done_tasks,
            global_iterations=self.global_iteration,
        )
        self.ui.print_success(
            f"Done! {done_tasks}/{total_tasks} tasks completed. Log: {self.log._path}"
        )

    def _run_loop(self) -> None:
        """Process tasks one by one until all are done."""
        self._needs_reparse = False

        while True:
            # Re-parse markdown if ARCH restructured tasks
            if self._needs_reparse:
                log.info(
                    "tasks.reloading",
                    reason="arch_redecompose",
                    task_file=str(self.task_file),
                )
                self.ui.print_info("🏗️ ARCHITECT restructured tasks — reloading...")
                self.phases = load_tasks(self.task_file)
                self._needs_reparse = False

                total_tasks = sum(p.total for p in self.phases)
                done_tasks = sum(p.completed for p in self.phases)
                self.ui.update_project(self.phases, self.phases[0])
                self.ui.print_info(
                    f"Reloaded: {total_tasks} tasks ({done_tasks} done, "
                    f"{total_tasks - done_tasks} remaining)"
                )

            pair = find_next_task(self.phases)
            if pair is None:
                log.info("all_tasks.complete")
                self.ui.update_actor(Actor.QA, "—", "All tasks complete! 🎉")
                time.sleep(1)
                break

            phase, task = pair
            self.current_phase = phase
            self.ui.update_project(self.phases, phase)
            self.ui.update_phase(phase)

            # Reset stuckness counters for new task
            if self._stuck_task_label != task.label:
                self._stuck_task_label = task.label
                self._consecutive_exhaustions = 0
                self._iterations_without_approval = 0
                self._pending_arch_check = False
                # Restore counters from previous runs if available
                self._restore_stuckness_from_log(task)

            log.info(
                "task.starting",
                task_label=task.label,
                task_text=task.text,
                phase=phase.title,
                phase_progress=f"{phase.completed}/{phase.total}",
                global_iteration=self.global_iteration,
            )

            # Log memory at task start
            task_snap = snapshot()
            if task_snap is not None:
                log.info(
                    "memory.task_start", task_label=task.label, **task_snap.to_dict()
                )
                self.ui.print_info(f"Memory: {format_snapshot_human(task_snap)}")

            self.ui.print_info(
                f"\n{'=' * 60}\n"
                f"Starting task {task.label}: {task.text}\n"
                f"Phase: {phase.title} ({phase.completed}/{phase.total})\n"
                f"{'=' * 60}"
            )

            # Rotate sessions if scope boundary changed or --new-session was set
            self._maybe_rotate_session(task)

            # Run the QA→Dev loop for this task
            self._process_task(phase, task)

    def _maybe_rotate_session(self, task: Task) -> None:
        """Rotate dev/qa session IDs when the scope boundary changes.

        Generates new session names when:
        - The scope key (phase/subphase/task) differs from the previous task, OR
        - The user passed ``--new-session`` (one-shot, resets after firing).

        When scope is PHASE, all tasks within the same ## heading share a session.
        When scope is SUBPHASE (default), tasks share a session within each ### group.
        When scope is TASK, every task gets a fresh session.
        """
        scope_key = _compute_scope_key(task, self.session_scope)
        should_rotate = False
        rotation_reason = ""

        if self._force_new_session:
            should_rotate = True
            rotation_reason = "force_new_session"
            self._force_new_session = False  # one-shot
            self.ui.print_info(f"[{task.label}] Forcing new session (--new-session)")
        elif scope_key != self._current_scope_key:
            should_rotate = True
            rotation_reason = "scope_boundary"
            if self._current_scope_key:
                self.ui.print_info(
                    f"[{task.label}] Session scope changed: "
                    f"{self._current_scope_key} → {scope_key} — rotating sessions"
                )

        if should_rotate:
            self.dev_session_name = _generate_session_id("dev")
            self.qa_session_name = _generate_session_id("qa")
            log.info(
                "session.rotated",
                task_label=task.label,
                old_scope_key=self._current_scope_key or "(initial)",
                new_scope_key=scope_key,
                reason=rotation_reason,
                dev_session=self.dev_session_name,
                qa_session=self.qa_session_name,
            )
            self.ui.print_info(
                f"[{task.label}] New dev session: {self.dev_session_name}"
            )
            self.ui.print_info(
                f"[{task.label}] New QA session:  {self.qa_session_name}"
            )

        self._current_scope_key = scope_key

    @staticmethod
    def _qa_has_real_question(qa_response: QAResponse) -> bool:
        """Check whether QA actually has a specific question for the user.

        QA sometimes emits ``needs_user_input`` with a vacuous feedback like
        "Review in progress" and no ``user_question``.  Opening the
        interactive chat loop in that case is useless — the user sees an
        empty prompt and has no idea what to answer.

        Returns True only when there is a concrete, non-trivial question.
        """
        question = (qa_response.user_question or "").strip()
        feedback = (qa_response.feedback or "").strip()

        if question and len(question) >= 10:
            return True

        # If feedback itself looks like a question (ends with '?')
        if feedback.endswith("?") and len(feedback) >= 15:
            return True

        return False

    def _interactive_chat_loop(
        self,
        task: Task,
        phase: Phase,
        qa_response: QAResponse,
        blocker_description: str = "",
    ) -> bool:
        """Run interactive chat loop between user and QA agent.

        Pauses the Live UI, shows QA's question, accepts user input,
        feeds it to QA for processing, and loops until QA says resolved
        or the user types /done or /skip.

        Returns True if resolved (continue pipeline), False if skipped.
        """
        # Pause Live so input() works without interference
        self.ui.pause()

        self.ui.print_chat_header(
            task.label, qa_response.user_question or qa_response.feedback
        )

        conversation_history = ""
        chat_turn = 0
        max_chat_turns = 20  # safety limit

        while chat_turn < max_chat_turns:
            chat_turn += 1
            self.global_iteration += 1

            # Get user input
            user_input = self.ui.prompt_user_input()

            if user_input is None:
                # /skip — user wants to move on
                self.ui.print_chat_skipped()
                self.ui.resume()
                return False

            if user_input == "":
                # /done — user believes issue is resolved
                self.ui.print_chat_resolved()
                self.ui.resume()
                return True

            # Build conversation history
            conversation_history += f"👤 User: {user_input}\n\n"

            # Send user's response to QA for processing
            self.ui.print_info(f"[{task.label}] Sending your response to QA...")

            chat_request = UserChatRequest(
                task_label=task.label,
                task_text=task.text,
                blocker_description=blocker_description,
                user_message=user_input,
                conversation_history=conversation_history,
                qa_session_id=self.qa_session_name,
                dev_session_id=self.dev_session_name,
            )

            chat_result = self._run_goose_with_ui(
                Actor.QA,
                task.label,
                recipe_path=self.qa_recipe,
                session_name=self.qa_session_name,
                params=chat_request.to_params(),
                max_turns=self.max_turns,
                timeout_secs=self.timeout_secs,
                model=self.model,
                provider=self.provider,
                cwd=self.cwd,
                detail="chat response",
            )

            chat_qa_response = _parse_qa_response(
                chat_result.raw_stdout, chat_result.parsed_json
            )

            # Log the chat exchange
            chat_entry = IterationEntry(
                timestamp=_now_iso(),
                iteration=self.global_iteration,
                actor=Actor.QA,
                task_label=task.label,
                status=TaskStatus.NEEDS_USER_INPUT,
                payload={
                    "chat_turn": chat_turn,
                    "user_message": user_input,
                    "qa_response": chat_qa_response.to_dict()
                    if chat_qa_response
                    else None,
                    "raw": chat_result.raw_stdout[:300],
                },
                json_blocks_found=chat_result.json_blocks_found,
                json_blocks_cascade=chat_result.json_blocks_cascade,
                assistant_turns=chat_result.assistant_turns,
                total_turns=chat_result.total_turns,
                output_chars=chat_result.output_chars,
            )
            self.log.append(chat_entry)
            self.ui.add_iteration(chat_entry)

            # QA timed out during chat — inform user and continue
            if chat_result.timed_out:
                timeout_minutes = self.timeout_secs / 60
                self.ui.print_warning(
                    f"[{task.label}] QA timed out after {timeout_minutes:.0f}min during chat — continuing"
                )
                chat_qa_response = None
                conversation_history += "🧪 QA: (timed out — no response)\n\n"
                continue
            elif chat_qa_response is None:
                # QA didn't return structured output in chat mode — show raw text
                self.ui.print_qa_chat_message(
                    chat_result.raw_stdout[:500]
                    if chat_result.raw_stdout
                    else "(no response)"
                )
                conversation_history += (
                    f"🧪 QA: {chat_result.raw_stdout or '(no response)'}\n\n"
                )
                continue

            conversation_history += f"🧪 QA: {chat_qa_response.feedback}\n\n"

            # Check if QA considers the issue resolved
            if chat_qa_response.decision == "approve":
                self.ui.print_qa_chat_message(f"✓ {chat_qa_response.feedback}")
                self.ui.print_chat_resolved()
                self.ui.resume()
                return True

            if chat_qa_response.decision == "needs_user_input":
                # QA has a follow-up question
                self.ui.print_qa_chat_message(chat_qa_response.feedback)
                if chat_qa_response.user_question:
                    self.ui.print_qa_chat_message(
                        f"Follow-up: {chat_qa_response.user_question}"
                    )
                continue

            # reject or other — QA gave guidance, show it and continue chatting
            self.ui.print_qa_chat_message(chat_qa_response.feedback)
            if chat_qa_response.concerns:
                for c in chat_qa_response.concerns[:3]:
                    self.ui.print_qa_chat_message(f"  • {c}")

        # Max chat turns reached
        self.ui.print_warning(
            f"[{task.label}] Max chat turns ({max_chat_turns}) reached. Resuming pipeline."
        )
        self.ui.resume()
        return True  # best effort — continue

    # ── Goose subprocess UI wrapper ────────────────────────────

    def _run_goose_with_ui(
        self,
        actor: Actor,
        task_label: str,
        *,
        recipe_path: str | Path,
        session_name: str,
        params: dict[str, str] | None = None,
        max_turns: int = 80,
        timeout_secs: int = 600,
        model: str | None = None,
        provider: str | None = None,
        cwd: str | Path | None = None,
        detail: str = "",
    ) -> GooseRunResult:
        """Run goose with UI activity indicator and pending iteration row.

        Wraps :func:`run_goose_with_backoff` so every agent launch gets visual feedback
        in both the header panel (elapsed timer) and the iteration log table
        (animated spinner row).
        """
        icon = "🧪" if actor == Actor.QA else ("🛠️" if actor == Actor.DEV else "🏗️")
        name = (
            "QA Reviewer"
            if actor == Actor.QA
            else ("Developer" if actor == Actor.DEV else "Architect")
        )
        label = f"{icon} {name} — Task {task_label}"
        if detail:
            label += f"  ({detail})"

        # Resolve fallback model based on actor role:
        #   DEV → fallback_dev (e.g. local ollama for coding)
        #   QA  → fallback_qa  (e.g. different cloud for reviews)
        fb: FallbackModel | None = None
        if actor == Actor.DEV:
            fb = self.rate_limit.fallback_dev
        elif actor == Actor.QA:
            fb = self.rate_limit.fallback_qa
        fallback_model = fb.model if fb else None
        fallback_provider = fb.provider if fb else None

        # Take a memory snapshot before launching goose
        snap_before = snapshot()
        if snap_before is not None:
            log.info("memory.before_goose", **snap_before.to_dict())

        self.ui.activity_start(label)
        self.ui.set_pending_iteration(actor, task_label, detail=detail)
        try:
            result = run_goose_with_backoff(
                recipe_path=recipe_path,
                session_name=session_name,
                params=params,
                max_turns=max_turns,
                timeout_secs=timeout_secs,
                model=model,
                provider=provider,
                cwd=cwd,
                rate_limit=self.rate_limit,
                memory_limit=None,  # auto-detect from /proc/meminfo
                fallback_model=fallback_model,
                fallback_provider=fallback_provider,
            )
        finally:
            self.ui.clear_pending_iteration()
            self.ui.activity_stop()

        # Take a memory snapshot after goose returns and log the delta
        snap_after = snapshot()
        if snap_after is not None:
            after_dict = snap_after.to_dict()
            log.info("memory.after_goose", **after_dict)
            if snap_before is not None:
                mem_delta = delta(snap_before, snap_after)
                log.info("memory.goose_delta", **mem_delta)
            self._last_memory_snapshot = snap_after
            # Show memory state in the UI so it's always visible
            self.ui.print_info(f"Memory: {format_snapshot_human(snap_after)}")

        return result

    # ── Task context file ──────────────────────────────────────

    _TASK_CONTEXT_FILENAME = ".tasker-context.json"

    def _write_task_context(self, task: Task) -> None:
        """Write a JSON file with the current task description to the project root.

        The agent recipe instructs the agent to read this file first when starting
        a task.  This gives the agent a durable "north star" that survives context
        compaction, session recovery, and malformed-output retry cycles.

        The file is removed when the task completes (approve / skip / max-iter).
        """
        import json

        cwd = self.cwd or Path(".")
        ctx_path = cwd / self._TASK_CONTEXT_FILENAME

        payload = {
            "task_label": task.label,
            "task_text": task.text,
            "phase_index": task.phase_index,
            "task_index": task.task_index,
            "subphase": task.subphase,
            "timestamp": _now_iso(),
        }

        ctx_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        log.debug("task_context.written", path=str(ctx_path), task_label=task.label)

    def _cleanup_task_context(self) -> None:
        """Remove the task context file after task completion."""
        cwd = self.cwd or Path(".")
        ctx_path = cwd / self._TASK_CONTEXT_FILENAME
        try:
            if ctx_path.exists():
                ctx_path.unlink()
                log.debug("task_context.cleaned_up", path=str(ctx_path))
        except OSError as exc:
            log.warning(
                "task_context.cleanup_failed", path=str(ctx_path), error=str(exc)
            )

    # ── VCS integration methods ────────────────────────────────

    def _scan_vcs_paths(self) -> list[Path]:
        """Scan all task texts for directory references not tracked by git.

        Iterates all tasks in ``self.phases``, extracts directory references
        via ``_extract_dir_refs``, and returns sorted list of unique root
        directories that are NOT inside a git working tree.
        """
        if self.vcs is None:
            return []

        assert self.cwd is not None  # VCS requires a working directory
        candidates: set[Path] = set()
        for phase in self.phases:
            for task in phase.tasks:
                refs = _extract_dir_refs(task.text, self.cwd)
                candidates.update(refs)

        if not candidates:
            return []

        import subprocess

        untracked: list[Path] = []
        for path in sorted(candidates):
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(path),
                )
                if not result.returncode == 0:
                    untracked.append(path)
            except (subprocess.TimeoutExpired, OSError):
                # If we can't check, assume it needs init
                untracked.append(path)

        return untracked

    def _vcs_begin_task(self, task: Task) -> None:
        """Create an isolated workspace for the task.

        Called at the start of each task when VCS is enabled.
        Sets task.base_ref and task.task_ref.

        In multi-repo mode, we skip feature-branch creation and
        just log a marker — each repo gets a simple ``git add -A &&
        git commit`` on approval.
        """
        if self._git_repos:
            log.info(
                "vcs.multi_repo_task_started",
                task_label=task.label,
                repos=[p.name for p in self._git_repos],
            )
            self.ui.print_info(
                f"[{task.label}] VCS: multi-repo mode "
                f"(will commit in {len(self._git_repos)} repos on approval)"
            )
            return
        if self.vcs is None:
            return
        try:
            self.vcs.begin_task(task, cwd=self.cwd)
            base_display = task.base_ref[:12] if task.base_ref else "?"
            task_display = task.task_ref[:12] if task.task_ref else "?"
            log.info(
                "vcs.task_started",
                task_label=task.label,
                base_ref=base_display,
                task_ref=task_display,
            )
            self.ui.print_info(
                f"[{task.label}] VCS: created task workspace "
                f"(base={base_display}, task={task_display})"
            )
        except RuntimeError as exc:
            log.warning("vcs.begin_task_failed", task_label=task.label, error=str(exc))
            self.ui.print_warning(
                f"[{task.label}] VCS: failed to begin task: {exc}. "
                f"Continuing without VCS."
            )
            self.vcs = None

    def _vcs_get_diff(self, task: Task) -> tuple[str, str]:
        """Get the diff for the current task.

        Called before QA review to provide context about what changed.

        Returns:
            A tuple of (project_context, diff_size_note).
            - project_context: the formatted diff string, or a message
              pointing to a temp file when the diff exceeds MAX_DIFF_SIZE.
            - diff_size_note: a human-readable note about the diff size
              for logging/UI, or empty string if no diff.
        """
        # ── Multi-repo mode ──
        if self._git_repos:
            diff = self._multi_repo_diff()
            if not diff:
                return "", ""
            diff_lines = diff.count("\n") + 1
            diff_bytes = len(diff.encode("utf-8", errors="replace"))

            MAX_DIFF_SIZE = 100_000  # 100 KB — same as single-repo path

            if diff_bytes <= MAX_DIFF_SIZE:
                project_context = (
                    f"## VCS Diff (multi-repo, task changes)\n```\n{diff}\n```"
                )
                size_note = f"({diff_lines} lines, {diff_bytes / 1024:.0f} KB)"
                return project_context, size_note

            # Diff too large — truncate to last 80 KB (recent changes most relevant)
            import tempfile

            truncated = diff[-80_000:] if diff_bytes > 80_000 else diff
            trunc_lines = truncated.count("\n") + 1
            project_context = (
                f"## VCS Diff (multi-repo, TRUNCATED)\n"
                f"Full diff: {diff_lines} lines, {diff_bytes / 1024:.0f} KB. "
                f"Showing last {trunc_lines} lines (most recent changes).\n\n"
                f"```\n{truncated}\n```"
            )
            size_note = (
                f"({diff_lines} lines, {diff_bytes / 1024:.0f} KB → "
                f"truncated to {trunc_lines} lines)"
            )
            log.warning(
                "vcs.multi_repo_diff_truncated",
                task_label=task.label,
                diff_bytes=diff_bytes,
                diff_lines=diff_lines,
                truncated_to=80_000,
            )
            return project_context, size_note

        # ── Single-repo mode ──
        if self.vcs is None:
            return "", ""

        try:
            diff = self.vcs.get_diff(task, cwd=self.cwd)
        except RuntimeError as exc:
            log.warning("vcs.get_diff_failed", task_label=task.label, error=str(exc))
            self.ui.print_warning(f"[{task.label}] VCS: failed to get diff: {exc}")
            return "", ""

        if not diff:
            return "", ""

        diff_lines = diff.count("\n") + 1
        diff_bytes = len(diff.encode("utf-8", errors="replace"))
        log.debug(
            "vcs.diff_obtained",
            task_label=task.label,
            diff_lines=diff_lines,
            diff_bytes=diff_bytes,
        )

        # Build the full project_context string
        project_context = f"## VCS Diff (task changes)\n```\n{diff}\n```"

        # Linux MAX_ARG_STRLEN = PAGE_SIZE * 32 = 128 KB per argument.
        # We use 100 KB as a conservative threshold (leaves room for escaping
        # overhead, other params, and environment).
        MAX_DIFF_SIZE = 100_000  # 100 KB

        if diff_bytes <= MAX_DIFF_SIZE:
            size_note = f"({diff_lines} lines, {diff_bytes / 1024:.0f} KB)"
            return project_context, size_note

        # Diff is too large for CLI args — write to a temp file
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".diff",
            prefix=f"tasker-{task.label}-",
            dir=str(self.cwd),
            delete=False,
            encoding="utf-8",
        )
        tmp.write(diff)
        tmp.close()
        diff_file = Path(tmp.name)

        size_kb = diff_bytes / 1024
        log.warning(
            "vcs.diff_too_large_for_argv",
            task_label=task.label,
            diff_bytes=diff_bytes,
            diff_lines=diff_lines,
            diff_file=str(diff_file),
            hint="Diff exceeds MAX_ARG_STRLEN; written to temp file instead",
        )
        self.ui.print_warning(
            f"[{task.label}] VCS diff is {size_kb:.0f} KB / {diff_lines} lines "
            f"— exceeds CLI arg limit. Written to {diff_file.name}"
        )

        # Return a context string that points QA to the file
        truncated_preview = diff[:4096]
        project_context = (
            f"## ⚠️ VCS Diff (task changes) — LARGE DIFF\n\n"
            f"The full VCS diff is **{size_kb:.0f} KB / {diff_lines} lines**, "
            f"which exceeds the CLI argument size limit (~128 KB).\n"
            f"It has been written to a file on disk so you can read it.\n\n"
            f"### Instructions\n"
            f"1. Read the diff file at: `{diff_file}`\n"
            f"2. Use your developer tools to examine the full diff.\n"
            f"3. A truncated preview is provided below for quick reference.\n\n"
            f"### Truncated Preview (first ~4 KB)\n"
            f"```\n{truncated_preview}\n```\n\n"
            f"[... remaining {diff_lines - truncated_preview.count(chr(10))} "
            f"lines available in `{diff_file.name}` ...]"
        )
        size_note = (
            f"({diff_lines} lines, {size_kb:.0f} KB → temp file {diff_file.name})"
        )
        return project_context, size_note

    def _vcs_commit_task(self, task: Task) -> None:
        """Commit the task's changes as a single clean commit.

        Called when QA approves the task.
        """
        if self._git_repos:
            self._multi_repo_commit(task)
            return
        if self.vcs is None:
            return
        try:
            self.vcs.commit_task(task, cwd=self.cwd)
            log.info("vcs.task_committed", task_label=task.label)
            self.ui.print_info(f"[{task.label}] VCS: task committed")
        except RuntimeError as exc:
            log.error("vcs.commit_failed", task_label=task.label, error=str(exc))
            self.ui.print_warning(f"[{task.label}] VCS: failed to commit task: {exc}")

    # ── Multi-repo VCS helpers ────────────────────────────────────

    def _discover_git_repos(self) -> list[Path]:
        """Find git repos in immediate subdirectories of CWD.

        Scans one level deep: ``CWD/*/``.  Returns paths whose
        ``.git/`` directory exists, sorted by name.
        """
        if self.cwd is None:
            return []

        repos: list[Path] = []
        for child in sorted(self.cwd.iterdir()):
            if not child.is_dir():
                continue
            if (child / ".git").exists():
                repos.append(child)

        log.info(
            "vcs.discovered_repos",
            count=len(repos),
            repos=[p.name for p in repos],
        )
        return repos

    def _multi_repo_commit(self, task: Task) -> None:
        """Commit changes in every discovered git repo that has them.

        For each repo with a dirty working tree, this:
        1. ``git add -A``
        2. ``git commit -m "<task_label>: <task_text_summary>"``

        The commit message uses the task label for easy identification.
        """
        import subprocess

        if not self._git_repos:
            return

        msg = f"tasker: {task.label}"
        committed: list[str] = []

        for repo in self._git_repos:
            try:
                # Check for changes
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=str(repo),
                )
                if status.returncode != 0 or not status.stdout.strip():
                    continue

                # Stage everything
                add_result = subprocess.run(
                    ["git", "add", "-A"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(repo),
                )
                if add_result.returncode != 0:
                    log.warning(
                        "vcs.multi_repo_add_failed",
                        task_label=task.label,
                        repo=repo.name,
                        stderr=add_result.stderr[:200],
                    )
                    continue

                # Commit
                result = subprocess.run(
                    ["git", "commit", "-m", msg, "--allow-empty"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(repo),
                )
                if result.returncode == 0:
                    committed.append(repo.name)
                    log.info(
                        "vcs.multi_repo_committed",
                        task_label=task.label,
                        repo=repo.name,
                    )
                else:
                    log.warning(
                        "vcs.multi_repo_commit_failed",
                        task_label=task.label,
                        repo=repo.name,
                        stderr=result.stderr[:200],
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                log.warning(
                    "vcs.multi_repo_error",
                    task_label=task.label,
                    repo=repo.name,
                    error=str(exc),
                )

        if committed:
            self.ui.print_info(
                f"[{task.label}] VCS: committed in {', '.join(committed)}"
            )
        else:
            self.ui.print_info(f"[{task.label}] VCS: no changes to commit")

    def _multi_repo_diff(self) -> str:
        """Aggregate ``git diff`` from all discovered repos with changes."""
        import subprocess

        parts: list[str] = []
        for repo in self._git_repos:
            try:
                result = subprocess.run(
                    ["git", "diff", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=str(repo),
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts.append(f"### {repo.name}/\n{result.stdout}")
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        return "\n\n".join(parts)

    def _finalize_task(self, phase: Phase, task: Task) -> None:
        """Mark a task as done, update the markdown file, then VCS-commit.

        The order matters: file update MUST run before _vcs_commit_task
        so that the [x] checkbox change is included in the VCS commit.
        Otherwise the markdown change lives only in the working tree and
        is never committed (git) or is lost on the next task's branch switch.
        """
        log.info("task.finalizing", task_label=task.label)
        self._cleanup_task_context()
        mark_done(self.task_file, task, self.phases)
        log.debug("task.file_updated", task_label=task.label, file=str(self.task_file))
        self._vcs_commit_task(task)
        self.ui.update_phase(phase)
        self.ui.update_project(self.phases, phase)
        log.info("task.finalized", task_label=task.label)

        # Log memory at task completion
        done_snap = snapshot()
        if done_snap is not None:
            log.info("memory.task_done", task_label=task.label, **done_snap.to_dict())
            if self._last_memory_snapshot is not None:
                mem_delta = delta(self._last_memory_snapshot, done_snap)
                log.info("memory.cumulative_delta", task_label=task.label, **mem_delta)

    def _skip_task(self, phase: Phase, task: Task) -> None:
        """Mark task as permanently failed and persist [~] to markdown.

        Called when max_iterations is reached without QA approval, or when
        the circuit breaker detects a structural failure (empty output).
        Writes ``[~]`` to the markdown file so the task is permanently
        skipped on subsequent tasker runs.
        """
        task.skipped = True
        mark_task_failed(task, self.phases, self.task_file)
        self._cleanup_task_context()
        log.warning(
            "task.skipped",
            task_label=task.label,
            reason="max_iterations_reached_without_approval",
        )
        self.ui.print_warning(
            f"[{task.label}] ⚠ Skipped (marked [~] in markdown) — "
            f"task will not be retried on next run."
        )

    def _decompose_task(self, task: Task) -> DecomposeResponse | None:
        """Ask the decomposer agent whether *task* should be split.

        Returns ``None`` when decomposition is disabled (no ``decompose_recipe``
        configured).  On parse failure after all recovery attempts, returns a
        fallback ``DecomposeResponse(should_decompose=False, ...)`` so that the
        task proceeds as a single unit rather than stalling.
        """
        if not self.decompose_recipe:
            return None

        self.ui.update_actor(Actor.QA, task.label, "analyzing task complexity")
        self.ui.print_info(f"[{task.label}] Decompose: analyzing task complexity...")

        decompose_session = _generate_session_id("decompose")

        # We give the decomposer fewer turns — it only needs to read specs
        # and output a JSON decision, not implement anything.
        max_turns = min(self.max_turns, 30)

        result = self._run_goose_with_ui(
            Actor.QA,
            task.label,
            recipe_path=self.decompose_recipe,
            session_name=decompose_session,
            params={
                "task_label": task.label,
                "task_text": task.text,
                "decompose_session_id": decompose_session,
            },
            max_turns=max_turns,
            detail="decompose",
        )

        parsed = result.parsed_json
        response = _parse_decompose_response(result.raw_stdout, parsed)

        if response is None:
            log.warning(
                "decompose.parse_failed",
                task_label=task.label,
                stderr=result.raw_stderr[:200] if result.raw_stderr else "",
            )
            self.ui.print_warning(
                f"[{task.label}] Decompose agent returned unparsable output — "
                f"proceeding with full task."
            )
            return DecomposeResponse(
                should_decompose=False,
                reason="Decompose agent failed to produce valid JSON.",
            )

        if response.should_decompose:
            self.ui.print_info(
                f"[{task.label}] Decompose: splitting into "
                f"{len(response.subtasks)} subtask(s): "
                + ", ".join(s.label for s in response.subtasks)
            )
        else:
            self.ui.print_info(
                f"[{task.label}] Decompose: no split needed — {response.reason}"
            )

        # Log the decomposition decision
        decompose_entry = IterationEntry(
            timestamp=_now_iso(),
            iteration=self.global_iteration,
            actor=Actor.QA,
            task_label=task.label,
            status=TaskStatus.ASSIGNED,
            payload=response.to_dict(),
            json_blocks_found=result.json_blocks_found,
            json_blocks_cascade=result.json_blocks_cascade,
            assistant_turns=result.assistant_turns,
            total_turns=result.total_turns,
            output_chars=result.output_chars,
        )
        self.log.append(decompose_entry)
        self.ui.add_iteration(decompose_entry)

        return response

        return response

    # ── Architect (ARCH) agent integration ──────────────────────

    def _build_error_summary(self, task: Task) -> str:
        """Build a concise summary of errors for the ARCH agent.

        Reads recent entries from the iteration log for this task and
        summarizes the failure patterns.
        """
        entries = self.log.read_all()
        task_entries = [e for e in entries if e.get("task_label") == task.label]

        if not task_entries:
            return "No iteration log entries found for this task."

        # Count by error type
        error_counts: dict[str, int] = {}
        last_errors: list[str] = []
        for entry in task_entries[-50:]:  # last 50 entries
            payload = entry.get("payload", {})
            status = entry.get("status", "")
            if status == "error":
                error_type = payload.get("error", "unknown")
                error_counts[error_type] = error_counts.get(error_type, 0) + 1
                if len(last_errors) < 5:
                    stage = payload.get("stage", "")
                    raw = entry.get("raw_output", "")[:100]
                    last_errors.append(f"  - {error_type} (stage={stage}): {raw}")

        total = len(task_entries)
        errors = sum(1 for e in task_entries if e.get("status") == "error")
        blocked = sum(1 for e in task_entries if e.get("status") == "blocked")

        summary_lines = [
            f"Total attempts: {total} ({errors} errors, {blocked} blocked)",
            "",
            "Error breakdown:",
        ]
        for error_type, count in sorted(error_counts.items(), key=lambda x: -x[1]):
            summary_lines.append(f"  - {error_type}: {count}")

        if last_errors:
            summary_lines.append("")
            summary_lines.append("Last errors:")
            summary_lines.extend(last_errors)

        return "\n".join(summary_lines)

    def _build_code_state_summary(self, task: Task) -> str:
        """Build a summary of what code exists on disk for the ARCH agent.

        Uses `find` and `wc` to count source files in the project's crates/
        directory. Keeps it brief so it doesn't blow up the CLI params.
        """
        import subprocess

        cwd = self.cwd or Path(".")
        crates_dir = cwd / "crates"

        if not crates_dir.exists():
            return "No crates/ directory found — project may not be scaffolded yet."

        try:
            # Count .rs files modified recently (last 30 min)
            result = subprocess.run(
                [
                    "find",
                    str(crates_dir),
                    "-name",
                    "*.rs",
                    "-mmin",
                    "-30",
                    "-type",
                    "f",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            recent_files = (
                result.stdout.strip().split("\n") if result.stdout.strip() else []
            )
            # Make paths relative to cwd
            recent_rel = [
                str(Path(f).relative_to(cwd)) for f in recent_files if f.strip()
            ]
        except (subprocess.TimeoutExpired, Exception):
            recent_rel = []

        try:
            # Count total .rs files
            result = subprocess.run(
                ["find", str(crates_dir), "-name", "*.rs", "-type", "f"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            total_rs = (
                len(result.stdout.strip().split("\n")) if result.stdout.strip() else 0
            )
        except (subprocess.TimeoutExpired, Exception):
            total_rs = 0

        lines = [
            f"Project has {total_rs} .rs files under crates/.",
        ]
        if recent_rel:
            lines.append(f"\nRecently modified files ({len(recent_rel)}):")
            for f in recent_rel[:10]:
                lines.append(f"  - {f}")
        else:
            lines.append("\nNo recently modified files.")

        return "\n".join(lines)

    def _run_arch(self, task: Task) -> ArchResponse | None:
        """Run the Architect agent to diagnose and unblock a stuck task.

        Returns None if ARCH is disabled or if the agent fails to produce
        a valid response.
        """
        if not self.arch_recipe:
            return None

        self.ui.update_actor(Actor.ARCH, task.label, "diagnosing stuck task")
        self.ui.print_info(f"[{task.label}] 🏗️ ARCHITECT: diagnosing stuck task...")
        log.info(
            "arch.invoked",
            task_label=task.label,
            consecutive_exhaustions=self._consecutive_exhaustions,
            iterations_without_approval=self._iterations_without_approval,
        )

        error_summary = self._build_error_summary(task)
        code_state = self._build_code_state_summary(task)

        arch_request = ArchRequest(
            task_label=task.label,
            task_text=task.text,
            error_summary=error_summary[:3000],  # cap to prevent ARG_MAX
            code_state=code_state[:2000],
        )

        arch_session = _generate_session_id("arch")

        arch_result = self._run_goose_with_ui(
            Actor.ARCH,
            task.label,
            recipe_path=self.arch_recipe,
            session_name=arch_session,
            params=arch_request.to_params(),
            max_turns=min(self.max_turns, 30),  # ARCH doesn't need many turns
            timeout_secs=self.timeout_secs,
            model=self.model,
            provider=self.provider,
            cwd=self.cwd,
            detail="stuckness diagnosis",
        )

        parsed = arch_result.parsed_json
        arch_response = _parse_arch_response(arch_result.raw_stdout, parsed)

        if arch_response is None:
            log.warning(
                "arch.parse_failed",
                task_label=task.label,
                stderr=arch_result.raw_stderr[:200] if arch_result.raw_stderr else "",
            )
            self.ui.print_warning(
                f"[{task.label}] ARCHITECT returned unparsable output — "
                f"treating as RETRY."
            )
            return ArchResponse(
                action=ArchAction.RETRY.value,
                reason="ARCH agent failed to produce valid JSON.",
            )

        log.info(
            "arch.response_parsed",
            task_label=task.label,
            action=arch_response.action,
            reason=arch_response.reason[:100],
        )

        # Log the ARCH decision
        arch_entry = IterationEntry(
            timestamp=_now_iso(),
            iteration=self.global_iteration,
            actor=Actor.ARCH,
            task_label=task.label,
            status=TaskStatus.ASSIGNED,
            payload=arch_response.to_dict(),
            json_blocks_found=arch_result.json_blocks_found,
            json_blocks_cascade=arch_result.json_blocks_cascade,
            assistant_turns=arch_result.assistant_turns,
            total_turns=arch_result.total_turns,
            output_chars=arch_result.output_chars,
        )
        self.log.append(arch_entry)
        self.ui.add_iteration(arch_entry)

        return arch_response

    def _apply_arch_decision(
        self,
        phase: Phase,
        task: Task,
        arch_response: ArchResponse,
    ) -> str | None:
        """Apply the ARCH agent's decision and return the new task text.

        Returns:
            - For CLARIFY: the new task text (loop continues with this text)
            - For REDECOMPOSE: None (tasks were restructured, caller must re-parse)
            - For SKIP: None (task marked done)
            - For RETRY: the original task text (loop continues)
        """
        action = arch_response.action

        self.ui.print_info(
            f"[{task.label}] 🏗️ ARCHITECT decision: {action} — "
            f"{arch_response.reason[:200]}"
        )

        if action == ArchAction.REDECOMPOSE.value:
            if not arch_response.subtasks:
                self.ui.print_warning(
                    f"[{task.label}] ARCHITECT said REDECOMPOSE but gave no subtasks. "
                    f"Falling back to RETRY."
                )
                return task.text

            labels = [s.label for s in arch_response.subtasks]
            texts = [s.text for s in arch_response.subtasks]

            self.ui.print_info(
                f"[{task.label}] ARCHITECT decomposing into "
                f"{len(texts)} subtask(s): {', '.join(labels)}"
            )

            insert_subtasks_dispatch(
                self.task_file,
                self.phases,
                task,
                subtask_labels=labels,
                subtask_texts=texts,
            )

            log.info(
                "arch.redecomposed",
                task_label=task.label,
                subtask_count=len(texts),
                subtask_labels=labels,
            )

            # Signal caller to re-parse and continue
            return None

        elif action == ArchAction.CLARIFY.value:
            new_text = arch_response.new_task_text or task.text
            rewrite_text(self.task_file, self.phases, task, new_text)

            log.info(
                "arch.clarified",
                task_label=task.label,
                old_text=task.text[:60],
                new_text=new_text[:60],
            )
            return new_text

        elif action == ArchAction.SKIP.value:
            self.ui.print_info(
                f"[{task.label}] ARCHITECT recommends SKIP: "
                f"{arch_response.reason[:200]}"
            )
            # Only QA approval can mark a task done — ARCH SKIP leaves it as [ ]
            self._skip_task(phase, task)

            log.info(
                "arch.skipped",
                task_label=task.label,
                reason=arch_response.reason[:200],
            )
            return None

        elif action == ArchAction.RETRY.value:
            self.ui.print_info(
                f"[{task.label}] ARCHITECT recommends RETRY: "
                f"{arch_response.reason[:200]}"
            )
            # Reset stuckness counters
            self._consecutive_exhaustions = 0
            self._iterations_without_approval = 0

            log.info(
                "arch.retry",
                task_label=task.label,
                reason=arch_response.reason[:200],
            )
            return task.text

        else:
            self.ui.print_warning(
                f"[{task.label}] ARCHITECT returned unknown action '{action}'. "
                f"Falling back to RETRY."
            )
            return task.text

    def _is_task_stuck(self, task: Task) -> bool:
        """Check if a task meets the stuckness criteria."""
        if self._stuck_task_label != task.label:
            # Different task — reset counters
            self._stuck_task_label = task.label
            self._consecutive_exhaustions = 0
            self._iterations_without_approval = 0
            self._pending_arch_check = False

        return (
            self._consecutive_exhaustions >= _STUCK_EXHAUSTION_THRESHOLD
            or self._iterations_without_approval >= _STUCK_ITERATION_THRESHOLD
        )

    def _restore_stuckness_from_log(self, task: Task) -> None:
        """Restore stuckness counters from JSONL log for a resumed task.

        Scans iteration history for the given task and replays counter state
        so that stuckness detection works across process restarts.
        """
        entries = self.log.read_all()
        task_entries = [e for e in entries if e.get("task_label") == task.label]

        if not task_entries:
            return

        exhaustions = 0
        iters_without_approval = 0

        for entry in task_entries:
            status = entry.get("status", "")
            actor = entry.get("actor", "")

            if status == "blocked" and actor == "dev":
                exhaustions += 1
            elif status == "approved":
                # QA approval resets everything
                exhaustions = 0
                iters_without_approval = 0
            elif status == "feedback" and actor == "qa":
                iters_without_approval += 1
            elif status == "done" and actor == "dev":
                # Dev done doesn't reset — QA still needs to approve
                pass

        self._consecutive_exhaustions = exhaustions
        self._iterations_without_approval = iters_without_approval
        log.info(
            "stuckness.restored_from_log",
            task_label=task.label,
            consecutive_exhaustions=exhaustions,
            iterations_without_approval=iters_without_approval,
            total_entries=len(task_entries),
        )

    def _truncate_feedback(self, feedback: str) -> str:
        """Truncate feedback to prevent ARG_MAX overflow and context pollution.

        Keeps only the last N feedback rounds and caps total length.
        """
        if len(feedback) <= _MAX_FEEDBACK_LENGTH:
            return feedback

        # Split by QA decision headers and keep last N rounds
        rounds = feedback.split("## QA Decision:")
        if len(rounds) > _MAX_FEEDBACK_ROUNDS:
            kept = rounds[-_MAX_FEEDBACK_ROUNDS:]
            truncated = "## QA Decision:".join(kept)
            truncated = "[... earlier feedback truncated ...]\n\n" + truncated
        else:
            truncated = feedback

        # Hard cap
        if len(truncated) > _MAX_FEEDBACK_LENGTH:
            truncated = truncated[-_MAX_FEEDBACK_LENGTH:]
            truncated = "[... truncated ...]\n" + truncated

        return truncated

    def _run_dev_with_recovery(
        self,
        task: Task,
        iteration: int,
        feedback: str | None,
        override_task_text: str | None = None,
    ) -> DevResponse:
        """Run the dev agent with graceful degradation on malformed output.

        Escalation: NORMAL(1) → CONTINUE×3 → SUBTASK×3 → SUMMARIZE(1)
        Returns the final DevResponse (may be blocked if all retries fail).
        """
        stage = RecoveryStage.NORMAL
        attempts_in_stage = 0

        log.info(
            "dev.recovery_start",
            task_label=task.label,
            iteration=iteration,
            has_feedback=feedback is not None,
        )

        truncation_detected = False
        consecutive_empty = 0  # circuit breaker counter for empty-output failures

        while True:
            attempts_in_stage += 1

            # Pick recovery instruction based on stage — always include task context
            # so the agent doesn't lose track of its task during recovery.
            recovery_instruction: str | None = None
            if stage == RecoveryStage.NORMAL and attempts_in_stage == 1:
                recovery_instruction = None  # first call, no recovery needed
            elif stage == RecoveryStage.CONTINUE:
                recovery_instruction = _with_task_context(
                    _RECOVERY_CONTINUE, task.label, task.text
                )
            elif stage == RecoveryStage.SUBTASK:
                base = (
                    _RECOVERY_TRUNCATION if truncation_detected else _RECOVERY_SUBTASK
                )
                recovery_instruction = _with_task_context(base, task.label, task.text)
            elif stage == RecoveryStage.SUMMARIZE:
                recovery_instruction = _with_task_context(
                    _RECOVERY_SUMMARIZE, task.label, task.text
                )
            elif stage == RecoveryStage.RESTART:
                recovery_instruction = _with_task_context(
                    _RECOVERY_RESTART, task.label, task.text
                )

            self.ui.update_actor(
                Actor.DEV,
                task.label,
                f"iteration {iteration}"
                + (f" [{stage.value}]" if stage != RecoveryStage.NORMAL else ""),
            )
            self.ui.print_info(
                f"[{task.label}] Dev call (stage={stage.value}, attempt={attempts_in_stage})..."
            )

            log.debug(
                "dev.call",
                task_label=task.label,
                stage=stage.value,
                attempt=f"{attempts_in_stage}/{stage.max_attempts}",
                iteration=iteration,
            )

            # On CONTINUE recovery, suppress task_text to prevent the agent
            # from re-engaging with the full task. It has session history and
            # already knows what to do — it just needs to output the JSON block.
            effective_task_text = (
                override_task_text if override_task_text else task.text
            )
            if stage == RecoveryStage.CONTINUE:
                effective_task_text = (
                    f"[Recovery mode — see your session history for the full task. "
                    f"Task: {task.label}]"
                )
            elif stage in (RecoveryStage.SUBTASK, RecoveryStage.SUMMARIZE):
                effective_task_text = (
                    f"[Recovery mode ({stage.value}) — do NOT re-read task or specs. "
                    f"Task: {task.label}]"
                )

            dev_request = DevRequest(
                task_label=task.label,
                task_text=effective_task_text,
                qa_session_id=self.qa_session_name,
                dev_session_id=self.dev_session_name,
                iteration=iteration,
                max_turns=self.max_turns,
                feedback=feedback,
                recovery_instruction=recovery_instruction,
            )

            dev_result = self._run_goose_with_ui(
                Actor.DEV,
                task.label,
                recipe_path=self.dev_recipe,
                session_name=self.dev_session_name,
                params=dev_request.to_params(),
                max_turns=self.max_turns,
                timeout_secs=self.timeout_secs,
                model=self.model,
                provider=self.provider,
                cwd=self.cwd,
            )

            # Log cascade event if multiple JSON blocks were found
            if dev_result.json_blocks_cascade:
                log.info(
                    "json.cascade",
                    task_label=task.label,
                    actor="dev",
                    blocks_found=dev_result.json_blocks_found,
                )

            # Check for subprocess failure (crash, timeout)
            if not dev_result.success:
                if dev_result.timed_out:
                    # Timeout — don't escalate through recovery stages.
                    # Instead, return a blocked response with timeout context
                    # so the caller can relaunch the agent with awareness.
                    timeout_minutes = self.timeout_secs / 60
                    log.warning(
                        "dev.timeout",
                        task_label=task.label,
                        duration=dev_result.duration_secs,
                        timeout_secs=self.timeout_secs,
                    )
                    self.ui.print_warning(
                        f"[{task.label}] Dev timed out after {timeout_minutes:.0f}min — "
                        f"will relaunch with timeout context"
                    )
                    dev_timeout_entry = IterationEntry(
                        timestamp=_now_iso(),
                        iteration=self.global_iteration,
                        actor=Actor.DEV,
                        task_label=task.label,
                        status=TaskStatus.ERROR,
                        payload={
                            "error": "timeout",
                            "duration": dev_result.duration_secs,
                        },
                        raw_output=dev_result.raw_stderr[:500],
                        json_blocks_found=dev_result.json_blocks_found,
                        json_blocks_cascade=dev_result.json_blocks_cascade,
                        assistant_turns=dev_result.assistant_turns,
                        total_turns=dev_result.total_turns,
                        output_chars=dev_result.output_chars,
                    )
                    self.log.append(dev_timeout_entry)
                    self.ui.add_iteration(dev_timeout_entry)
                    return DevResponse(
                        status="blocked",
                        summary=f"Developer agent timed out after {timeout_minutes:.0f} minutes.",
                        files_modified=[],
                        notes="The developer process was killed due to timeout. "
                        "It will be relaunched with awareness of the timeout.",
                        blocker_description=_timeout_feedback(
                            "Developer", self.timeout_secs
                        ),
                        blocker_suggestion="The process was stuck. Review what you were doing "
                        "and finish quickly. Do NOT redo completed work.",
                    )

                # Other subprocess failures (crash, etc.)
                log.error(
                    "dev.subprocess_failed",
                    task_label=task.label,
                    return_code=dev_result.return_code,
                    stderr=dev_result.raw_stderr[:200],
                )
                self.ui.print_error(
                    f"[{task.label}] Dev subprocess failed (rc={dev_result.return_code}): "
                    f"{dev_result.raw_stderr[:200]}"
                )
                dev_crash_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.DEV,
                    task_label=task.label,
                    status=TaskStatus.ERROR,
                    payload={"error": "subprocess_failed", "stage": stage.value},
                    raw_output=dev_result.raw_stderr[:500],
                    json_blocks_found=dev_result.json_blocks_found,
                    json_blocks_cascade=dev_result.json_blocks_cascade,
                    assistant_turns=dev_result.assistant_turns,
                    total_turns=dev_result.total_turns,
                    output_chars=dev_result.output_chars,
                )
                self.log.append(dev_crash_entry)
                self.ui.add_iteration(dev_crash_entry)

                # Connection-error hard stop: if the provider is down, don't
                # waste recovery budget on retries. Skip straight to blocked.
                stderr_lower = (dev_result.raw_stderr or "").lower()
                if "not connected" in stderr_lower:
                    log.warning(
                        "dev.connection_error_hard_stop",
                        task_label=task.label,
                        stage=stage.value,
                    )
                    self.ui.print_warning(
                        f"[{task.label}] Connection error detected — "
                        f"skipping remaining retries"
                    )
                    break  # exit recovery loop → synthetic blocked
                # Subprocess failures don't count as malformed — try again in same stage
                if attempts_in_stage < stage.max_attempts:
                    self.ui.print_warning(
                        f"[{task.label}] Retrying ({attempts_in_stage}/{stage.max_attempts})..."
                    )
                    continue
                # Exhausted this stage, escalate
                if stage == RecoveryStage.RESTART:
                    break
                if stage == RecoveryStage.SUMMARIZE:
                    stage = RecoveryStage.RESTART
                elif stage == RecoveryStage.CONTINUE:
                    stage = RecoveryStage.SUBTASK
                else:
                    stage = RecoveryStage.SUMMARIZE
                # Rotate dev session when entering RESTART to clear stale context
                if stage == RecoveryStage.RESTART:
                    old_dev_session = self.dev_session_name
                    self.dev_session_name = _generate_session_id("dev")
                    log.info(
                        "session.rotated",
                        task_label=task.label,
                        reason="recovery_restart",
                        dev_session=self.dev_session_name,
                        qa_session=self.qa_session_name,
                    )
                    self.ui.print_info(
                        f"[{task.label}] Dev session rotated for RESTART stage: "
                        f"{old_dev_session} → {self.dev_session_name}"
                    )
                attempts_in_stage = 0
                continue

            # Try to parse the structured response
            dev_response = _parse_dev_response(
                dev_result.raw_stdout, dev_result.parsed_json
            )

            if dev_response is not None:
                # Parsed successfully — log and return
                log.info(
                    "dev.response_parsed",
                    task_label=task.label,
                    status=dev_response.status,
                    summary=dev_response.summary[:100],
                    files=dev_response.files_modified,
                    stage=stage.value,
                    attempt=attempts_in_stage,
                )
                dev_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.DEV,
                    task_label=task.label,
                    status=TaskStatus.BLOCKED
                    if dev_response.status == "blocked"
                    else TaskStatus.IN_PROGRESS,
                    payload=dev_response.to_dict(),
                    raw_output=dev_result.raw_stdout[:500],
                    json_blocks_found=dev_result.json_blocks_found,
                    json_blocks_cascade=dev_result.json_blocks_cascade,
                    assistant_turns=dev_result.assistant_turns,
                    total_turns=dev_result.total_turns,
                    output_chars=dev_result.output_chars,
                )
                # Check for checkpoint signal
                if _is_checkpoint(dev_response):
                    log.info(
                        "dev.checkpoint",
                        task_label=task.label,
                        checkpoint_summary=dev_response.summary[:200],
                    )
                    dev_entry.checkpoint = True

                self.log.append(dev_entry)
                self.ui.add_iteration(dev_entry)
                return dev_response

            # Malformed output — log the failure
            log.warning(
                "dev.malformed_output",
                task_label=task.label,
                stage=stage.value,
                attempt=f"{attempts_in_stage}/{stage.max_attempts}",
                empty_output=dev_result.empty_output,
            )
            self.ui.print_warning(
                f"[{task.label}] Dev did not return valid JSON (stage={stage.value}, "
                f"attempt={attempts_in_stage}/{stage.max_attempts})"
            )

            # Circuit breaker: if goose produced completely empty output,
            # no amount of re-prompting will help — the failure is structural
            # (goose never reached the LLM).  Count and break early.
            if dev_result.empty_output:
                consecutive_empty += 1
                if consecutive_empty >= self.max_consecutive_empty:
                    log.error(
                        "dev.empty_output_circuit_breaker",
                        task_label=task.label,
                        consecutive_empty=consecutive_empty,
                        max_consecutive_empty=self.max_consecutive_empty,
                    )
                    self.ui.print_error(
                        f"[{task.label}] Circuit breaker: {consecutive_empty} consecutive "
                        f"empty outputs — skipping remaining recovery stages"
                    )
                    break  # exit recovery loop → synthetic blocked
            else:
                consecutive_empty = 0  # reset on non-empty malformed output

            malformed_entry = IterationEntry(
                timestamp=_now_iso(),
                iteration=self.global_iteration,
                actor=Actor.DEV,
                task_label=task.label,
                status=TaskStatus.ERROR,
                payload={
                    "error": "malformed_output",
                    "stage": stage.value,
                    "empty_output": dev_result.empty_output,
                },
                raw_output=dev_result.raw_stdout[:500],
                json_blocks_found=dev_result.json_blocks_found,
                json_blocks_cascade=dev_result.json_blocks_cascade,
                assistant_turns=dev_result.assistant_turns,
                total_turns=dev_result.total_turns,
                output_chars=dev_result.output_chars,
            )
            self.log.append(malformed_entry)
            self.ui.add_iteration(malformed_entry)

            # Truncation fast-forward: if the output was cut off mid-tool-call,
            # skip ahead to SUBTASK stage with truncation-specific guidance.
            if _is_truncated_output(dev_result.raw_stdout):
                if stage not in (
                    RecoveryStage.SUBTASK,
                    RecoveryStage.SUMMARIZE,
                    RecoveryStage.RESTART,
                ):
                    log.warning(
                        "dev.truncation_detected",
                        task_label=task.label,
                        from_stage=stage.value,
                    )
                    self.ui.print_warning(
                        f"[{task.label}] Truncation detected — "
                        f"fast-forwarding to SUBTASK stage"
                    )
                    stage = RecoveryStage.SUBTASK
                    truncation_detected = True
                    attempts_in_stage = 0
                    continue

            # Escalation logic
            if attempts_in_stage >= stage.max_attempts:
                if stage == RecoveryStage.RESTART:
                    break  # all stages exhausted
                # Move to next stage
                old_stage = stage
                if stage == RecoveryStage.NORMAL:
                    stage = RecoveryStage.CONTINUE
                elif stage == RecoveryStage.CONTINUE:
                    stage = RecoveryStage.SUBTASK
                elif stage == RecoveryStage.SUBTASK:
                    stage = RecoveryStage.SUMMARIZE
                elif stage == RecoveryStage.SUMMARIZE:
                    stage = RecoveryStage.RESTART
                attempts_in_stage = 0
                # Rotate dev session when entering RESTART to clear stale context
                if stage == RecoveryStage.RESTART:
                    old_dev_session = self.dev_session_name
                    self.dev_session_name = _generate_session_id("dev")
                    log.info(
                        "session.rotated",
                        task_label=task.label,
                        reason="recovery_restart",
                        dev_session=self.dev_session_name,
                        qa_session=self.qa_session_name,
                    )
                    self.ui.print_info(
                        f"[{task.label}] Dev session rotated for RESTART stage: "
                        f"{old_dev_session} → {self.dev_session_name}"
                    )
                self.ui.print_warning(
                    f"[{task.label}] Escalating to stage: {stage.value}"
                )
                log.warning(
                    "dev.escalating",
                    task_label=task.label,
                    from_stage=old_stage.value,
                    to_stage=stage.value,
                )

        # All stages exhausted — return a synthetic blocked response
        log.error(
            "dev.recovery_exhausted",
            task_label=task.label,
            iteration=iteration,
        )
        self.ui.print_error(
            f"[{task.label}] All recovery attempts exhausted. "
            f"Returning synthetic blocked response."
        )
        synthetic = DevResponse(
            status="blocked",
            summary="Developer failed to produce a valid response after multiple recovery attempts.",
            files_modified=[],
            notes="The developer agent could not complete the task. "
            "All recovery stages (continue, subtask, summarize, restart) were exhausted.",
            blocker_description="Developer agent returned malformed output across all recovery attempts. "
            "The task may be too complex, poorly specified, or the agent may be "
            "encountering tooling issues.",
            blocker_suggestion="Consider breaking this task into smaller, more specific subtasks. "
            "Or review if the task description is clear and complete.",
        )
        synthetic_blocked_entry = IterationEntry(
            timestamp=_now_iso(),
            iteration=self.global_iteration,
            actor=Actor.DEV,
            task_label=task.label,
            status=TaskStatus.BLOCKED,
            payload=synthetic.to_dict(),
            json_blocks_found=0,
            json_blocks_cascade=False,
            assistant_turns=0,
            total_turns=0,
            output_chars=0,
        )
        self.log.append(synthetic_blocked_entry)
        self.ui.add_iteration(synthetic_blocked_entry)
        return synthetic

    def _run_qa_with_recovery(
        self,
        task: Task,
        iteration: int,
        qa_request: QARequest,
        *,
        detail: str = "",
    ) -> QAResponse:
        """Run the QA agent with graceful degradation on malformed output.

        Escalation: NORMAL(1) → CONTINUE×3 → SUMMARIZE×3 → RESTART(1)
        Returns the final QAResponse (may be a synthetic reject if all retries fail).
        """
        stage = QARecoveryStage.NORMAL
        attempts_in_stage = 0
        consecutive_empty = 0  # circuit breaker counter for empty-output failures

        log.info(
            "qa.recovery_start",
            task_label=task.label,
            iteration=iteration,
            detail=detail,
        )

        while True:
            attempts_in_stage += 1

            # Pick recovery instruction based on stage — include task context
            # so QA doesn't lose track of what it's reviewing during recovery.
            recovery_instruction: str | None = None
            if stage == QARecoveryStage.NORMAL and attempts_in_stage == 1:
                recovery_instruction = None  # first call, no recovery needed
            elif stage == QARecoveryStage.CONTINUE:
                recovery_instruction = _with_task_context(
                    _QA_RECOVERY_CONTINUE, task.label, task.text
                )
            elif stage == QARecoveryStage.SUMMARIZE:
                recovery_instruction = _with_task_context(
                    _QA_RECOVERY_SUMMARIZE, task.label, task.text
                )
            elif stage == QARecoveryStage.RESTART:
                recovery_instruction = _with_task_context(
                    _QA_RECOVERY_RESTART, task.label, task.text
                )

            # Inject recovery instruction into QA params
            params = qa_request.to_params()
            if recovery_instruction:
                # QA recipe doesn't have a dedicated recovery_instruction param,
                # so we append it to the dev_notes field which QA reads.
                params["dev_notes"] = (
                    f"{params.get('dev_notes', '')}\n\n"
                    f"## ⚠️ Format Recovery ({stage.value})\n"
                    f"{recovery_instruction}"
                ).strip()
                # On CONTINUE, suppress task_text to prevent re-reading specs.
                # QA already has session history with the full review context.
                if stage == QARecoveryStage.CONTINUE:
                    params["task_text"] = (
                        f"[Recovery mode — see your session history. Task: {task.label}]"
                    )

            self.ui.update_actor(
                Actor.QA,
                task.label,
                f"reviewing iteration {iteration}"
                + (f" [{stage.value}]" if stage != QARecoveryStage.NORMAL else ""),
            )
            self.ui.print_info(
                f"[{task.label}] QA call (stage={stage.value}, "
                f"attempt={attempts_in_stage}, detail={detail or 'review'})..."
            )

            log.debug(
                "qa.call",
                task_label=task.label,
                stage=stage.value,
                attempt=f"{attempts_in_stage}/{stage.max_attempts}",
                iteration=iteration,
                detail=detail,
            )

            qa_result = self._run_goose_with_ui(
                Actor.QA,
                task.label,
                recipe_path=self.qa_recipe,
                session_name=self.qa_session_name,
                params=params,
                max_turns=self.max_turns,
                timeout_secs=self.timeout_secs,
                model=self.model,
                provider=self.provider,
                cwd=self.cwd,
                detail=detail or f"review [{stage.value}]",
            )

            # Log cascade event if multiple JSON blocks were found
            if qa_result.json_blocks_cascade:
                log.info(
                    "json.cascade",
                    task_label=task.label,
                    actor="qa",
                    blocks_found=qa_result.json_blocks_found,
                )

            # Check for subprocess failure (crash, timeout)
            if not qa_result.success:
                if qa_result.timed_out:
                    timeout_minutes = self.timeout_secs / 60
                    log.warning(
                        "qa.timeout",
                        task_label=task.label,
                        iteration=iteration,
                        context=detail,
                        duration=qa_result.duration_secs,
                    )
                    self.ui.print_warning(
                        f"[{task.label}] QA timed out after {timeout_minutes:.0f}min "
                        f"(detail={detail}) — treating as rejection"
                    )
                    qa_timeout_entry = IterationEntry(
                        timestamp=_now_iso(),
                        iteration=self.global_iteration,
                        actor=Actor.QA,
                        task_label=task.label,
                        status=TaskStatus.ERROR,
                        payload={
                            "error": "timeout",
                            "duration": qa_result.duration_secs,
                            "stage": stage.value,
                        },
                        raw_output=qa_result.raw_stderr[:500],
                        json_blocks_found=qa_result.json_blocks_found,
                        json_blocks_cascade=qa_result.json_blocks_cascade,
                        assistant_turns=qa_result.assistant_turns,
                        total_turns=qa_result.total_turns,
                        output_chars=qa_result.output_chars,
                    )
                    self.log.append(qa_timeout_entry)
                    self.ui.add_iteration(qa_timeout_entry)
                    # Timeout doesn't escalate — return synthetic reject
                    return QAResponse(
                        decision="reject",
                        feedback=_timeout_feedback("QA", self.timeout_secs),
                        concerns=["QA agent timed out during review"],
                    )

                # Other subprocess failures (crash, etc.)
                log.error(
                    "qa.subprocess_failed",
                    task_label=task.label,
                    return_code=qa_result.return_code,
                    stderr=qa_result.raw_stderr[:200],
                    stage=stage.value,
                )
                self.ui.print_error(
                    f"[{task.label}] QA subprocess failed (rc={qa_result.return_code}): "
                    f"{qa_result.raw_stderr[:200]}"
                )
                qa_crash_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.QA,
                    task_label=task.label,
                    status=TaskStatus.ERROR,
                    payload={
                        "error": "subprocess_failed",
                        "stage": stage.value,
                    },
                    raw_output=qa_result.raw_stderr[:500],
                    json_blocks_found=qa_result.json_blocks_found,
                    json_blocks_cascade=qa_result.json_blocks_cascade,
                    assistant_turns=qa_result.assistant_turns,
                    total_turns=qa_result.total_turns,
                    output_chars=qa_result.output_chars,
                )
                self.log.append(qa_crash_entry)
                self.ui.add_iteration(qa_crash_entry)
                # Subprocess failures don't count as malformed — retry in same stage
                if attempts_in_stage < stage.max_attempts:
                    self.ui.print_warning(
                        f"[{task.label}] QA retrying "
                        f"({attempts_in_stage}/{stage.max_attempts})..."
                    )
                    continue
                # Exhausted this stage, escalate
                if stage == QARecoveryStage.RESTART:
                    break
                old_stage = stage
                stage = (
                    QARecoveryStage.RESTART
                )  # skip SUMMARIZE on crash, go straight to RESTART
                attempts_in_stage = 0
                # Rotate QA session when entering RESTART
                old_qa_session = self.qa_session_name
                self.qa_session_name = _generate_session_id("qa")
                log.info(
                    "session.rotated",
                    task_label=task.label,
                    reason="qa_recovery_restart",
                    dev_session=self.dev_session_name,
                    qa_session=self.qa_session_name,
                )
                self.ui.print_info(
                    f"[{task.label}] QA session rotated for RESTART stage: "
                    f"{old_qa_session} → {self.qa_session_name}"
                )
                self.ui.print_warning(
                    f"[{task.label}] QA escalating to stage: {stage.value}"
                )
                log.warning(
                    "qa.escalating",
                    task_label=task.label,
                    from_stage=old_stage.value,
                    to_stage=stage.value,
                )
                continue

            # Try to parse the structured response
            qa_response = _parse_qa_response(
                qa_result.raw_stdout, qa_result.parsed_json
            )

            if qa_response is not None:
                # Parsed successfully — log and return
                log.info(
                    "qa.response_parsed",
                    task_label=task.label,
                    decision=qa_response.decision,
                    feedback=qa_response.feedback[:100],
                    stage=stage.value,
                    attempt=attempts_in_stage,
                )
                qa_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.QA,
                    task_label=task.label,
                    status=(
                        TaskStatus.APPROVED
                        if qa_response.decision == "approve"
                        else TaskStatus.FEEDBACK
                    ),
                    payload=qa_response.to_dict(),
                    raw_output=qa_result.raw_stdout[:500],
                    json_blocks_found=qa_result.json_blocks_found,
                    json_blocks_cascade=qa_result.json_blocks_cascade,
                    assistant_turns=qa_result.assistant_turns,
                    total_turns=qa_result.total_turns,
                    output_chars=qa_result.output_chars,
                )
                # Check for checkpoint signal
                if _is_checkpoint(qa_response):
                    log.info(
                        "qa.checkpoint",
                        task_label=task.label,
                        checkpoint_summary=qa_response.feedback[:200],
                    )
                    qa_entry.checkpoint = True

                self.log.append(qa_entry)
                self.ui.add_iteration(qa_entry)
                return qa_response

            # Malformed output — log the failure
            log.warning(
                "qa.malformed_output",
                task_label=task.label,
                stage=stage.value,
                attempt=f"{attempts_in_stage}/{stage.max_attempts}",
                empty_output=qa_result.empty_output,
            )
            self.ui.print_warning(
                f"[{task.label}] QA did not return valid JSON (stage={stage.value}, "
                f"attempt={attempts_in_stage}/{stage.max_attempts})"
            )

            # Circuit breaker: if goose produced completely empty output,
            # no amount of re-prompting will help.
            if qa_result.empty_output:
                consecutive_empty += 1
                if consecutive_empty >= self.max_consecutive_empty:
                    log.error(
                        "qa.empty_output_circuit_breaker",
                        task_label=task.label,
                        consecutive_empty=consecutive_empty,
                        max_consecutive_empty=self.max_consecutive_empty,
                    )
                    self.ui.print_error(
                        f"[{task.label}] QA circuit breaker: {consecutive_empty} consecutive "
                        f"empty outputs — skipping remaining recovery stages"
                    )
                    break  # exit recovery loop → synthetic reject
            else:
                consecutive_empty = 0

            malformed_entry = IterationEntry(
                timestamp=_now_iso(),
                iteration=self.global_iteration,
                actor=Actor.QA,
                task_label=task.label,
                status=TaskStatus.ERROR,
                payload={
                    "error": "malformed_output",
                    "stage": stage.value,
                    "empty_output": qa_result.empty_output,
                },
                raw_output=qa_result.raw_stdout[:500],
                json_blocks_found=qa_result.json_blocks_found,
                json_blocks_cascade=qa_result.json_blocks_cascade,
                assistant_turns=qa_result.assistant_turns,
                total_turns=qa_result.total_turns,
                output_chars=qa_result.output_chars,
            )
            self.log.append(malformed_entry)
            self.ui.add_iteration(malformed_entry)

            # Truncation fast-forward: if QA output was truncated, skip to SUMMARIZE
            if _is_truncated_output(qa_result.raw_stdout):
                if stage not in (QARecoveryStage.SUMMARIZE, QARecoveryStage.RESTART):
                    log.warning(
                        "qa.truncation_detected",
                        task_label=task.label,
                        from_stage=stage.value,
                    )
                    self.ui.print_warning(
                        f"[{task.label}] QA truncation detected — "
                        f"fast-forwarding to SUMMARIZE stage"
                    )
                    stage = QARecoveryStage.SUMMARIZE
                    attempts_in_stage = 0
                    continue

            # Escalation logic
            if attempts_in_stage >= stage.max_attempts:
                if stage == QARecoveryStage.RESTART:
                    break  # all stages exhausted
                # Move to next stage
                old_stage = stage
                if stage == QARecoveryStage.NORMAL:
                    stage = QARecoveryStage.CONTINUE
                elif stage == QARecoveryStage.CONTINUE:
                    stage = QARecoveryStage.SUMMARIZE
                elif stage == QARecoveryStage.SUMMARIZE:
                    stage = QARecoveryStage.RESTART
                attempts_in_stage = 0
                # Rotate QA session when entering RESTART to clear stale context
                if stage == QARecoveryStage.RESTART:
                    old_qa_session = self.qa_session_name
                    self.qa_session_name = _generate_session_id("qa")
                    log.info(
                        "session.rotated",
                        task_label=task.label,
                        reason="qa_recovery_restart",
                        dev_session=self.dev_session_name,
                        qa_session=self.qa_session_name,
                    )
                    self.ui.print_info(
                        f"[{task.label}] QA session rotated for RESTART stage: "
                        f"{old_qa_session} → {self.qa_session_name}"
                    )
                self.ui.print_warning(
                    f"[{task.label}] QA escalating to stage: {stage.value}"
                )
                log.warning(
                    "qa.escalating",
                    task_label=task.label,
                    from_stage=old_stage.value,
                    to_stage=stage.value,
                )

        # All stages exhausted — return a synthetic reject with any raw output as feedback
        log.error(
            "qa.recovery_exhausted",
            task_label=task.label,
            iteration=iteration,
        )
        self.ui.print_error(
            f"[{task.label}] All QA recovery attempts exhausted. "
            f"Returning synthetic reject response."
        )
        synthetic = QAResponse(
            decision="reject",
            feedback=(
                "QA agent failed to produce a valid structured response after "
                "multiple recovery attempts. Please review your work carefully "
                "and ensure it meets the task requirements."
            ),
            concerns=[
                "QA agent could not complete its review — all recovery stages exhausted"
            ],
        )
        synthetic_reject_entry = IterationEntry(
            timestamp=_now_iso(),
            iteration=self.global_iteration,
            actor=Actor.QA,
            task_label=task.label,
            status=TaskStatus.FEEDBACK,
            payload=synthetic.to_dict(),
            json_blocks_found=0,
            json_blocks_cascade=False,
            assistant_turns=0,
            total_turns=0,
            output_chars=0,
        )
        self.log.append(synthetic_reject_entry)
        self.ui.add_iteration(synthetic_reject_entry)
        return synthetic

    def _process_task(self, phase: Phase, task: Task) -> None:
        """Run QA→Dev feedback loop for a single task.

        If decomposition is enabled, the task is first analyzed by the
        decomposer agent.  When it returns subtasks, each subtask is run
        through its own DEV→QA loop.  Otherwise the original task text is
        passed to DEV unchanged.
        """
        # Write task context file so agents can re-orient after recovery
        self._write_task_context(task)

        # Begin jj change for this task (covers all subtasks)
        self._vcs_begin_task(task)

        # ── Pre-scan: ask decomposer whether to split ──
        decomposition = self._decompose_task(task)
        if decomposition and decomposition.should_decompose and decomposition.subtasks:
            self._run_subtask_loop(phase, task, decomposition.subtasks)
        else:
            self._run_feedback_loop(phase, task, task.text, feedback=None)

    def _run_subtask_loop(
        self, phase: Phase, task: Task, subtasks: list[Subtask]
    ) -> None:
        """Run DEV→QA loop for each subtask in sequence.

        All subtasks share the same DEV and QA sessions so context
        accumulates across subtasks (important for dependencies).
        Feedback from a rejected subtask does NOT carry over to the
        next subtask — each is independently verified.
        """
        total = len(subtasks)
        for idx, subtask in enumerate(subtasks, 1):
            self.ui.print_info(f"[{task.label}] Subtask {idx}/{total}: {subtask.label}")
            log.info(
                "subtask.start",
                task_label=task.label,
                subtask_label=subtask.label,
                subtask_index=f"{idx}/{total}",
            )

            self._run_feedback_loop(
                phase,
                task,
                subtask.text,
                feedback=None,
                subtask_label=subtask.label,
                subtask_index=f"{idx}/{total}",
            )

    def _run_feedback_loop(
        self,
        phase: Phase,
        task: Task,
        effective_task_text: str,
        feedback: str | None,
        subtask_label: str | None = None,
        subtask_index: str | None = None,
    ) -> None:
        """Run the DEV→QA feedback loop for a single task or subtask.

        *effective_task_text* is the text sent to DEV (either the original
        task text or a focused subtask description).  The *task* object
        itself is used for VCS tracking, markdown marking, and logging.
        """
        # The label shown to the user and agents for this unit of work
        work_label = subtask_label or task.label

        # feedback parameter carries prior feedback; reset on first iteration
        # (already set from parameter)

        log.info(
            "feedback_loop.start",
            task_label=task.label,
            max_iterations=self.max_iterations,
        )

        for iteration in range(1, self.max_iterations + 1):
            self.global_iteration += 1

            # ── Log memory state at the start of each iteration ──
            iter_snap = snapshot()
            if iter_snap is not None:
                log.info(
                    "memory.iteration_start",
                    iteration=iteration,
                    global_iteration=self.global_iteration,
                    **iter_snap.to_dict(),
                )

            # ── Deferred stuckness check: invoke ARCH if flagged last iteration ──
            if self._pending_arch_check and self.arch_recipe:
                self._pending_arch_check = False
                self.ui.print_warning(
                    f"[{work_label}] ⚠️ Task appears stuck "
                    f"(exhaustions={self._consecutive_exhaustions}, "
                    f"iterations_without_approval={self._iterations_without_approval}). "
                    f"Invoking ARCHITECT..."
                )
                log.warning(
                    "feedback_loop.stuck",
                    task_label=task.label,
                    iteration=iteration,
                    consecutive_exhaustions=self._consecutive_exhaustions,
                    iterations_without_approval=self._iterations_without_approval,
                )

                arch_response = self._run_arch(task)
                if arch_response is not None:
                    new_text = self._apply_arch_decision(phase, task, arch_response)

                    if new_text is None:
                        # REDECOMPOSE (tasks restructured) or SKIP (task done)
                        # Signal caller to re-parse markdown
                        self._needs_reparse = True
                        return

                    # CLARIFY or RETRY — update effective task text
                    effective_task_text = new_text
                    self._consecutive_exhaustions = 0
                    self._iterations_without_approval = 0

                    # Rotate sessions for a fresh start
                    self.dev_session_name = _generate_session_id("dev")
                    self.qa_session_name = _generate_session_id("qa")
                    log.info(
                        "session.rotated",
                        task_label=task.label,
                        reason="arch_intervention",
                        dev_session=self.dev_session_name,
                        qa_session=self.qa_session_name,
                    )

            # ── 1. Assign to DEV (with recovery) ──
            dev_response = self._run_dev_with_recovery(
                task=task,
                iteration=iteration,
                feedback=feedback,
                override_task_text=effective_task_text,
            )

            # ── Validate "done" claim: check VCS diff (Bug Fix 7) ──
            if dev_response.status == "done" and self.vcs is not None:
                # _vcs_get_diff returns ("", "") when diff is empty
                _vcs_check, _ = self._vcs_get_diff(task)
                if not _vcs_check:
                    log.warning(
                        "dev.empty_diff_on_done",
                        task_label=task.label,
                        iteration=iteration,
                    )
                    self.ui.print_warning(
                        f"[{work_label}] ⚠️ Dev claims done but VCS diff is empty. "
                        f"Downgrading to blocked."
                    )
                    dev_response = DevResponse(
                        status="blocked",
                        summary="No file changes detected despite claiming done. "
                        "The task may already be complete from a previous session, "
                        "or no actual code was written.",
                        files_modified=[],
                        notes="Auto-downgraded from 'done' due to empty VCS diff.",
                    )

            # ── Handle dev started (checkpoint-only response) ──
            # When the agent emitted only the checkpoint JSON and ran out of
            # turns before producing a final answer, cascade extraction returns
            # the "started" checkpoint as the sole JSON block.  This is NOT a
            # blocker — the agent simply didn't finish.  Treat it as a soft
            # "incomplete" so the next iteration retries (recovery will handle
            # it), but do NOT increment stuckness or trigger QA triage.
            if dev_response.status == "started":
                log.info(
                    "dev.checkpoint_only",
                    task_label=task.label,
                    iteration=iteration,
                    note="Agent emitted checkpoint but no final JSON — will retry",
                )
                self.ui.print_info(
                    f"[{work_label}] Dev emitted checkpoint only (no final JSON). "
                    f"Will retry with recovery guidance."
                )
                # Do NOT increment stuckness counter — this is expected on long tasks
                # Continue to next iteration with recovery feedback
                feedback = (
                    "## ⚠️ Checkpoint-Only Response\n\n"
                    "Your previous invocation emitted the initial checkpoint JSON "
                    'but did NOT produce a final JSON block with `status: "done"` '
                    'or `status: "blocked"`.\n\n'
                    "This usually means you ran out of turns before finishing.\n\n"
                    "**What to do:**\n"
                    "1. Pick up where you left off — do NOT start over.\n"
                    "2. Focus on finishing quickly.\n"
                    "3. Output the final JSON block as soon as possible.\n"
                )
                # Don't count this toward stuckness — it's a transient turn-budget issue
                continue  # back to top of iteration loop → dev retry

            # ── Handle dev blocked ──
            if dev_response.status == "blocked":
                # Track stuckness: ANY blocked event counts toward exhaustion.
                # Previously only synthetic blocked (from recovery exhaustion)
                # was counted, but real blocked events also indicate a stuck task.
                self._consecutive_exhaustions += 1
                log.info(
                    "dev.stuckness_counter_incremented",
                    task_label=task.label,
                    consecutive_exhaustions=self._consecutive_exhaustions,
                    iterations_without_approval=self._iterations_without_approval,
                )

                log.warning(
                    "dev.blocked",
                    task_label=task.label,
                    iteration=iteration,
                    blocker=dev_response.blocker_description[:200],
                    suggestion=dev_response.blocker_suggestion[:200]
                    if dev_response.blocker_suggestion
                    else None,
                )
                self.ui.print_warning(
                    f"[{work_label}] Developer BLOCKED: {dev_response.blocker_description[:200]}"
                )
                if dev_response.blocker_suggestion:
                    self.ui.print_info(
                        f"[{work_label}] Dev suggestion: {dev_response.blocker_suggestion[:200]}"
                    )

                # Send blocker to QA for triage
                self.ui.update_actor(
                    Actor.QA, work_label, f"triaging blocker (iteration {iteration})"
                )
                self.ui.print_info(f"[{work_label}] Asking QA to triage blocker...")

                qa_blocked_request = QARequest(
                    task_label=work_label,
                    task_text=effective_task_text,
                    dev_response=dev_response,
                    dev_session_id=self.dev_session_name,
                    qa_session_id=self.qa_session_name,
                    iteration=iteration,
                    dev_blocked=True,
                    blocker_description=dev_response.blocker_description,
                    max_turns=self.max_turns,
                )

                qa_response = self._run_qa_with_recovery(
                    task=task,
                    iteration=iteration,
                    qa_request=qa_blocked_request,
                    detail="blocker triage",
                )

                # Log QA triage (already logged inside _run_qa_with_recovery,
                # but add a BLOCKED status entry for the iteration table)
                qa_blocked_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.QA,
                    task_label=task.label,
                    status=TaskStatus.BLOCKED,
                    payload=qa_response.to_dict(),
                    json_blocks_found=0,
                    json_blocks_cascade=False,
                    assistant_turns=0,
                    total_turns=0,
                    output_chars=0,
                )
                self.log.append(qa_blocked_entry)
                self.ui.add_iteration(qa_blocked_entry)

                if qa_response.decision == "needs_user_input":
                    if not self._qa_has_real_question(qa_response):
                        # Vacuous question — downgrade to reject so dev retries
                        log.warning(
                            "qa.needs_user_input_downgraded",
                            task_label=task.label,
                            feedback=qa_response.feedback[:100],
                        )
                        self.ui.print_warning(
                            f"[{task.label}] QA requested user input but "
                            f"provided no question — treating as reject"
                        )
                        feedback = (
                            f"## QA Feedback\n\n{qa_response.feedback}\n\n"
                            f"QA indicated it needs user input but did not "
                            f"formulate a specific question. Please review "
                            f"and address the feedback above.\n"
                        )
                        continue

                    log.info(
                        "qa.needs_user_input",
                        task_label=task.label,
                        iteration=iteration,
                        question=qa_response.user_question[:200]
                        if qa_response.user_question
                        else None,
                    )
                    resolved = self._interactive_chat_loop(
                        task=task,
                        phase=phase,
                        qa_response=qa_response,
                        blocker_description=dev_response.blocker_description,
                    )
                    if not resolved:
                        # User skipped — only QA can mark done; leave as [ ] for retry
                        self._skip_task(phase, task)
                        return
                    # Issue resolved via chat — loop back so dev retries
                    feedback = (
                        "## Blocker Resolved via User Chat\n\n"
                        "The blocker was discussed with the user and resolved. "
                        "Please proceed with the task.\n"
                    )
                    continue  # back to top of iteration loop

                elif qa_response.decision == "approve":
                    # QA decided the blocker is acceptable (e.g., task is already partially done)
                    self.ui.print_success(
                        f"[{work_label}] QA approved blocked task: {qa_response.feedback[:100]}"
                    )
                    self._finalize_task(phase, task)
                    return  # Bug Fix 5: exit immediately — no further code must run

                else:
                    # reject — QA gave guidance to unblock the dev, loop back
                    self.ui.print_warning(
                        f"[{work_label}] QA triage: try again with guidance: "
                        f"{qa_response.feedback[:200]}"
                    )
                    feedback = (
                        f"## Developer Blocker\n\n"
                        f"**Blocker**: {dev_response.blocker_description}\n"
                        f"**Dev suggestion**: {dev_response.blocker_suggestion}\n\n"
                        f"## QA Triage Guidance\n\n"
                        f"{qa_response.feedback}\n\n"
                    )
                    for c in qa_response.concerns:
                        feedback += f"- {c}\n"
                    feedback += (
                        "\nPlease address the blocker and the QA guidance above."
                    )
                    feedback = self._truncate_feedback(feedback)
                    # ── Evaluate stuckness after counters updated ──
                    if self._is_task_stuck(task):
                        self._pending_arch_check = True
                    continue  # back to top of iteration loop → dev retry

            self.ui.print_info(
                f"[{work_label}] Developer done: {dev_response.summary[:100]}"
            )

            log.info(
                "dev.done",
                task_label=task.label,
                iteration=iteration,
                summary=dev_response.summary[:100],
            )

            # ── 2. Send to QA for review ──
            self.ui.update_actor(
                Actor.QA, work_label, f"reviewing iteration {iteration}"
            )
            self.ui.print_info(f"[{work_label}] Iteration {iteration}: calling QA...")

            # Get VCS diff for QA context (may be truncated or written to
            # a temp file if the diff exceeds the OS per-argument size limit)
            vcs_context, diff_size_note = self._vcs_get_diff(task)
            if diff_size_note:
                self.ui.print_info(f"[{work_label}] VCS diff: {diff_size_note}")

            log.debug(
                "qa.call",
                task_label=task.label,
                iteration=iteration,
                has_vcs_diff=bool(vcs_context),
            )

            qa_request = QARequest(
                task_label=work_label,
                task_text=effective_task_text,
                dev_response=dev_response,
                dev_session_id=self.dev_session_name,
                qa_session_id=self.qa_session_name,
                iteration=iteration,
                project_context=vcs_context,
                max_turns=self.max_turns,
            )

            qa_response = self._run_qa_with_recovery(
                task=task,
                iteration=iteration,
                qa_request=qa_request,
                detail="review",
            )

            if qa_response.decision == "approve":
                log.info(
                    "qa.approved",
                    task_label=task.label,
                    iteration=iteration,
                    feedback=qa_response.feedback[:100],
                )
                self.ui.print_success(
                    f"[{work_label}] ✓ APPROVED by QA: {qa_response.feedback[:100]}"
                )
                self._finalize_task(phase, task)
                return

            elif qa_response.decision == "needs_user_input":
                if not self._qa_has_real_question(qa_response):
                    # Vacuous question — downgrade to reject so dev retries
                    log.warning(
                        "qa.needs_user_input_downgraded",
                        task_label=task.label,
                        feedback=qa_response.feedback[:100],
                    )
                    self.ui.print_warning(
                        f"[{work_label}] QA requested user input but "
                        f"provided no question — treating as reject"
                    )
                    self._iterations_without_approval += 1
                    feedback = (
                        f"## QA Feedback (no specific question)\n\n"
                        f"{qa_response.feedback}\n\n"
                        f"QA indicated it needs user input but did not "
                        f"formulate a specific question. Please review "
                        f"and address the feedback above, then try again.\n"
                    )
                    if qa_response.concerns:
                        feedback += "\n### Concerns\n"
                        for c in qa_response.concerns:
                            feedback += f"- {c}\n"
                    continue

                resolved = self._interactive_chat_loop(
                    task=task,
                    phase=phase,
                    qa_response=qa_response,
                )
                if not resolved:
                    # User skipped — only QA can mark done; leave as [ ] for retry
                    self._skip_task(phase, task)
                    return
                # Issue resolved via chat — loop back so dev retries with updated context
                feedback = (
                    "## Issue Resolved via User Chat\n\n"
                    "An issue was discussed with the user and resolved. "
                    "Please continue the task.\n"
                )
                continue  # back to top of iteration loop

            else:
                # reject
                self._iterations_without_approval += 1

                log.info(
                    "qa.rejected",
                    task_label=task.label,
                    iteration=iteration,
                    feedback=qa_response.feedback[:200],
                    num_concerns=len(qa_response.concerns),
                )
                self.ui.print_warning(
                    f"[{work_label}] ✗ REJECTED by QA: {qa_response.feedback[:200]}"
                )
                if qa_response.concerns:
                    for c in qa_response.concerns[:5]:
                        self.ui.print_warning(f"  • {c}")
                feedback = (
                    f"## QA Decision: REJECT\n\n"
                    f"**Feedback:** {qa_response.feedback}\n\n"
                    f"**Concerns:**\n"
                )
                for c in qa_response.concerns:
                    feedback += f"- {c}\n"
                feedback += "\nPlease fix ALL concerns above and re-submit."
                feedback = self._truncate_feedback(feedback)

                # ── Evaluate stuckness after counters updated ──
                if self._is_task_stuck(task):
                    self._pending_arch_check = True

        # Max iterations reached without QA approval — consult ARCH before giving up
        log.error(
            "feedback_loop.max_iterations",
            task_label=task.label,
            max_iterations=self.max_iterations,
        )
        self.ui.print_error(
            f"[{work_label}] Max iterations ({self.max_iterations}) reached without QA "
            f"approval. "
            + (
                "Requesting ARCHITECT review to improve task for next attempt..."
                if self.arch_recipe
                else "Skipping task — it remains [ ] for retry."
            )
        )

        if self.arch_recipe:
            arch_response = self._run_arch(task)
            if arch_response is not None:
                new_text = self._apply_arch_decision(phase, task, arch_response)
                if new_text is None:
                    # REDECOMPOSE: subtasks inserted, original marked [x] by insert_subtasks
                    # SKIP: _apply_arch_decision already called _skip_task
                    self._needs_reparse = True
                    max_iter_entry = IterationEntry(
                        timestamp=_now_iso(),
                        iteration=self.global_iteration,
                        actor=Actor.ARCH,
                        task_label=task.label,
                        status=TaskStatus.ERROR,
                        payload={
                            "error": "max_iterations_reached",
                            "arch_action": arch_response.action,
                        },
                        json_blocks_found=0,
                        json_blocks_cascade=False,
                        assistant_turns=0,
                        total_turns=0,
                        output_chars=0,
                    )
                    self.log.append(max_iter_entry)
                    self.ui.add_iteration(max_iter_entry)
                    return
                # CLARIFY or RETRY — task text updated in file, skip and retry next run
                self.ui.print_info(
                    f"[{work_label}] ARCHITECT improved task description. "
                    f"Will retry with updated text next run."
                )

        self._skip_task(phase, task)
        max_iter_entry = IterationEntry(
            timestamp=_now_iso(),
            iteration=self.global_iteration,
            actor=Actor.QA,
            task_label=task.label,
            status=TaskStatus.ERROR,
            payload={"error": "max_iterations_reached"},
            json_blocks_found=0,
            json_blocks_cascade=False,
            assistant_turns=0,
            total_turns=0,
            output_chars=0,
        )
        self.log.append(max_iter_entry)
        self.ui.add_iteration(max_iter_entry)
