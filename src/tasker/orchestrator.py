"""QA↔Dev orchestrator — the core feedback loop."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .goose import GooseRunResult, run_goose
from .log import IterationLog
from .models import (
    Actor,
    DevRequest,
    DevResponse,
    IterationEntry,
    Phase,
    QAResponse,
    QARequest,
    RecoveryStage,
    SessionScope,
    Task,
    TaskStatus,
    UserChatRequest,
)
from .parser import find_next_task, mark_task_done, parse_task_file, update_markdown
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
        status = parsed["status"]
        if status not in ("done", "blocked"):
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


# ── Recovery instructions for graceful degradation ────────────────

_RECOVERY_CONTINUE = (
    "Your previous response did not include the required JSON block. "
    "Continue from exactly where you left off and finish the task. "
    "You MUST end your response with the JSON block in the exact format specified."
)

_RECOVERY_SUBTASK = (
    "Your previous responses did not include the required JSON block. "
    "The task may be too large. Break it into smaller subtasks. "
    "Implement just the FIRST subtask now, then respond with the JSON block. "
    "In your summary, list all subtasks you identified and which one you completed."
)

_RECOVERY_SUMMARIZE = (
    "Your previous responses have not produced the required JSON block. "
    "STOP implementing. Instead, write a brief summary of: "
    "1) What progress you have made so far, "
    "2) What difficulties you are encountering, "
    "3) What still needs to be done. "
    "Then respond with the JSON block using status 'blocked'."
)


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
    ) -> None:
        self.task_file = Path(task_file).resolve()
        self.dev_recipe = Path(dev_recipe)
        self.qa_recipe = Path(qa_recipe)
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

        # Session scope — controls when new goose sessions are created
        self.session_scope = session_scope
        self._current_scope_key: str = ""  # tracks the current scope boundary
        self._force_new_session = force_new_session  # one-shot flag

        # goose run uses --name for session persistence and auto-resumes
        # when the same name is used again.
        self.dev_session_name = _generate_session_id("dev")
        self.qa_session_name = _generate_session_id("qa")

        # State
        self.phases: list[Phase] = []
        self.current_phase: Phase | None = None
        self.global_iteration = 0

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
        )
        self.ui.print_info(f"Developer session: {self.dev_session_name}")
        self.ui.print_info(f"QA session:       {self.qa_session_name}")
        self.ui.print_info(f"Session scope:    {self.session_scope.value}")
        self.ui.print_info(f"Iteration log:    {self.log._path}")
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
                    self.ui.print_error(f"VCS init failed: {exc}")
                    self.vcs = None

        # Parse tasks
        self.phases = parse_task_file(self.task_file)

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

        total_tasks = sum(p.total for p in self.phases)
        done_tasks = sum(p.completed for p in self.phases)
        log.info(
            "tasks.loaded",
            phases=len(self.phases),
            total_tasks=total_tasks,
            done_tasks=done_tasks,
            remaining=total_tasks - done_tasks,
        )
        self.ui.print_info(
            f"Loaded {len(self.phases)} phases, {total_tasks} tasks "
            f"({done_tasks} already done, {total_tasks - done_tasks} remaining)"
        )
        self.ui.print_info("")

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
        while True:
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

            log.info(
                "task.starting",
                task_label=task.label,
                task_text=task.text,
                phase=phase.title,
                phase_progress=f"{phase.completed}/{phase.total}",
                global_iteration=self.global_iteration,
            )

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

        Wraps :func:`run_goose` so every agent launch gets visual feedback
        in both the header panel (elapsed timer) and the iteration log table
        (animated spinner row).
        """
        icon = "🧪" if actor == Actor.QA else "🛠️"
        name = "QA Reviewer" if actor == Actor.QA else "Developer"
        label = f"{icon} {name} — Task {task_label}"
        if detail:
            label += f"  ({detail})"
        self.ui.activity_start(label)
        self.ui.set_pending_iteration(actor, task_label, detail=detail)
        try:
            result = run_goose(
                recipe_path=recipe_path,
                session_name=session_name,
                params=params,
                max_turns=max_turns,
                timeout_secs=timeout_secs,
                model=model,
                provider=provider,
                cwd=cwd,
            )
        finally:
            self.ui.clear_pending_iteration()
            self.ui.activity_stop()
        return result

    # ── VCS integration methods ────────────────────────────────

    def _vcs_begin_task(self, task: Task) -> None:
        """Create an isolated workspace for the task.

        Called at the start of each task when VCS is enabled.
        Sets task.base_ref and task.task_ref.
        """
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

    def _vcs_get_diff(self, task: Task) -> str:
        """Get the diff for the current task.

        Called before QA review to provide context about what changed.
        """
        if self.vcs is None:
            return ""
        try:
            diff = self.vcs.get_diff(task, cwd=self.cwd)
            log.debug(
                "vcs.diff_obtained",
                task_label=task.label,
                diff_lines=diff.count("\n") if diff else 0,
            )
            return diff
        except RuntimeError as exc:
            log.warning("vcs.get_diff_failed", task_label=task.label, error=str(exc))
            self.ui.print_warning(f"[{task.label}] VCS: failed to get diff: {exc}")
            return ""

    def _vcs_commit_task(self, task: Task) -> None:
        """Commit the task's changes as a single clean commit.

        Called when QA approves the task.
        """
        if self.vcs is None:
            return
        try:
            self.vcs.commit_task(task, cwd=self.cwd)
            log.info("vcs.task_committed", task_label=task.label)
            self.ui.print_info(f"[{task.label}] VCS: task committed")
        except RuntimeError as exc:
            log.error("vcs.commit_failed", task_label=task.label, error=str(exc))
            self.ui.print_warning(f"[{task.label}] VCS: failed to commit task: {exc}")

    def _finalize_task(self, phase: Phase, task: Task) -> None:
        """Mark a task as done, update the markdown file, then VCS-commit.

        The order matters: update_markdown MUST run before _vcs_commit_task
        so that the [x] checkbox change is included in the VCS commit.
        Otherwise the markdown change lives only in the working tree and
        is never committed (git) or is lost on the next task's branch switch.
        """
        log.info("task.finalizing", task_label=task.label)
        mark_task_done(task, self.phases)
        update_markdown(self.task_file, self.phases)
        log.debug(
            "task.markdown_updated", task_label=task.label, file=str(self.task_file)
        )
        self._vcs_commit_task(task)
        self.ui.update_phase(phase)
        self.ui.update_project(self.phases, phase)
        log.info("task.finalized", task_label=task.label)

    def _run_dev_with_recovery(
        self,
        task: Task,
        iteration: int,
        feedback: str | None,
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

        while True:
            attempts_in_stage += 1

            # Pick recovery instruction based on stage
            recovery_instruction: str | None = None
            if stage == RecoveryStage.NORMAL and attempts_in_stage == 1:
                recovery_instruction = None  # first call, no recovery needed
            elif stage == RecoveryStage.CONTINUE:
                recovery_instruction = _RECOVERY_CONTINUE
            elif stage == RecoveryStage.SUBTASK:
                recovery_instruction = _RECOVERY_SUBTASK
            elif stage == RecoveryStage.SUMMARIZE:
                recovery_instruction = _RECOVERY_SUMMARIZE

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

            dev_request = DevRequest(
                task_label=task.label,
                task_text=task.text,
                qa_session_id=self.qa_session_name,
                dev_session_id=self.dev_session_name,
                iteration=iteration,
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
                )
                self.log.append(dev_crash_entry)
                self.ui.add_iteration(dev_crash_entry)
                # Subprocess failures don't count as malformed — try again in same stage
                if attempts_in_stage < stage.max_attempts:
                    self.ui.print_warning(
                        f"[{task.label}] Retrying ({attempts_in_stage}/{stage.max_attempts})..."
                    )
                    continue
                # Exhausted this stage, escalate
                if stage == RecoveryStage.SUMMARIZE:
                    break
                stage = (
                    RecoveryStage.SUBTASK
                    if stage == RecoveryStage.CONTINUE
                    else RecoveryStage.SUMMARIZE
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
                )
                self.log.append(dev_entry)
                self.ui.add_iteration(dev_entry)
                return dev_response

            # Malformed output — log the failure
            log.warning(
                "dev.malformed_output",
                task_label=task.label,
                stage=stage.value,
                attempt=f"{attempts_in_stage}/{stage.max_attempts}",
            )
            self.ui.print_warning(
                f"[{task.label}] Dev did not return valid JSON (stage={stage.value}, "
                f"attempt={attempts_in_stage}/{stage.max_attempts})"
            )
            malformed_entry = IterationEntry(
                timestamp=_now_iso(),
                iteration=self.global_iteration,
                actor=Actor.DEV,
                task_label=task.label,
                status=TaskStatus.ERROR,
                payload={"error": "malformed_output", "stage": stage.value},
                raw_output=dev_result.raw_stdout[:500],
            )
            self.log.append(malformed_entry)
            self.ui.add_iteration(malformed_entry)

            # Escalation logic
            if attempts_in_stage >= stage.max_attempts:
                if stage == RecoveryStage.SUMMARIZE:
                    break  # all stages exhausted
                # Move to next stage
                old_stage = stage
                if stage == RecoveryStage.NORMAL:
                    stage = RecoveryStage.CONTINUE
                elif stage == RecoveryStage.CONTINUE:
                    stage = RecoveryStage.SUBTASK
                elif stage == RecoveryStage.SUBTASK:
                    stage = RecoveryStage.SUMMARIZE
                attempts_in_stage = 0
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
            "All recovery stages (continue, subtask, summarize) were exhausted.",
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
        )
        self.log.append(synthetic_blocked_entry)
        self.ui.add_iteration(synthetic_blocked_entry)
        return synthetic

    def _process_task(self, phase: Phase, task: Task) -> None:
        """Run QA→Dev feedback loop for a single task."""
        # Begin jj change for this task
        self._vcs_begin_task(task)

        feedback: str | None = None  # None on first iteration

        log.info(
            "feedback_loop.start",
            task_label=task.label,
            max_iterations=self.max_iterations,
        )

        for iteration in range(1, self.max_iterations + 1):
            self.global_iteration += 1

            # ── 1. Assign to DEV (with recovery) ──
            dev_response = self._run_dev_with_recovery(
                task=task,
                iteration=iteration,
                feedback=feedback,
            )

            # ── Handle dev blocked ──
            if dev_response.status == "blocked":
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
                    f"[{task.label}] Developer BLOCKED: {dev_response.blocker_description[:200]}"
                )
                if dev_response.blocker_suggestion:
                    self.ui.print_info(
                        f"[{task.label}] Dev suggestion: {dev_response.blocker_suggestion[:200]}"
                    )

                # Send blocker to QA for triage
                self.ui.update_actor(
                    Actor.QA, task.label, f"triaging blocker (iteration {iteration})"
                )
                self.ui.print_info(f"[{task.label}] Asking QA to triage blocker...")

                qa_blocked_request = QARequest(
                    task_label=task.label,
                    task_text=task.text,
                    dev_response=dev_response,
                    dev_session_id=self.dev_session_name,
                    qa_session_id=self.qa_session_name,
                    iteration=iteration,
                    dev_blocked=True,
                    blocker_description=dev_response.blocker_description,
                )

                qa_result = self._run_goose_with_ui(
                    Actor.QA,
                    task.label,
                    recipe_path=self.qa_recipe,
                    session_name=self.qa_session_name,
                    params=qa_blocked_request.to_params(),
                    max_turns=self.max_turns,
                    timeout_secs=self.timeout_secs,
                    model=self.model,
                    provider=self.provider,
                    cwd=self.cwd,
                    detail="blocker triage",
                )

                qa_response = _parse_qa_response(
                    qa_result.raw_stdout, qa_result.parsed_json
                )

                # QA timed out — treat as rejection with timeout context
                if qa_result.timed_out:
                    timeout_minutes = self.timeout_secs / 60
                    log.warning(
                        "qa.timeout",
                        task_label=task.label,
                        iteration=iteration,
                        context="blocker_triage",
                        duration=qa_result.duration_secs,
                    )
                    self.ui.print_warning(
                        f"[{task.label}] QA timed out after {timeout_minutes:.0f}min during blocker triage — treating as rejection"
                    )
                    qa_triage_timeout_entry = IterationEntry(
                        timestamp=_now_iso(),
                        iteration=self.global_iteration,
                        actor=Actor.QA,
                        task_label=task.label,
                        status=TaskStatus.ERROR,
                        payload={
                            "error": "timeout",
                            "duration": qa_result.duration_secs,
                        },
                        raw_output=qa_result.raw_stderr[:500],
                    )
                    self.log.append(qa_triage_timeout_entry)
                    self.ui.add_iteration(qa_triage_timeout_entry)
                    qa_response = QAResponse(
                        decision="reject",
                        feedback=_timeout_feedback("QA", self.timeout_secs),
                        concerns=["QA agent timed out during blocker triage"],
                    )

                if qa_response is None:
                    qa_response = QAResponse(
                        decision="reject",
                        feedback="QA could not triage the blocker — no structured response.",
                        concerns=[
                            "QA did not return structured JSON for blocker triage"
                        ],
                    )

                # Log QA triage
                qa_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.QA,
                    task_label=task.label,
                    status=TaskStatus.BLOCKED,
                    payload=qa_response.to_dict(),
                    raw_output=qa_result.raw_stdout[:500],
                )
                self.log.append(qa_entry)
                self.ui.add_iteration(qa_entry)

                if qa_response.decision == "needs_user_input":
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
                        # User skipped — mark done and move on
                        self._finalize_task(phase, task)
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
                        f"[{task.label}] QA approved blocked task: {qa_response.feedback[:100]}"
                    )
                    self._finalize_task(phase, task)
                    return

                else:
                    # reject — QA gave guidance to unblock the dev, loop back
                    self.ui.print_warning(
                        f"[{task.label}] QA triage: try again with guidance: "
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
                    continue  # back to top of iteration loop → dev retry

            self.ui.print_info(
                f"[{task.label}] Developer done: {dev_response.summary[:100]}"
            )

            log.info(
                "dev.done",
                task_label=task.label,
                iteration=iteration,
                summary=dev_response.summary[:100],
            )

            # ── 2. Send to QA for review ──
            self.ui.update_actor(
                Actor.QA, task.label, f"reviewing iteration {iteration}"
            )
            self.ui.print_info(f"[{task.label}] Iteration {iteration}: calling QA...")

            # Get VCS diff for QA context
            vcs_diff = self._vcs_get_diff(task)

            log.debug(
                "qa.call",
                task_label=task.label,
                iteration=iteration,
                has_vcs_diff=bool(vcs_diff),
            )

            qa_request = QARequest(
                task_label=task.label,
                task_text=task.text,
                dev_response=dev_response,
                dev_session_id=self.dev_session_name,
                qa_session_id=self.qa_session_name,
                iteration=iteration,
                project_context=f"## VCS Diff (task changes)\n```\n{vcs_diff}\n```"
                if vcs_diff
                else "",
            )

            qa_result = self._run_goose_with_ui(
                Actor.QA,
                task.label,
                recipe_path=self.qa_recipe,
                session_name=self.qa_session_name,
                params=qa_request.to_params(),
                max_turns=self.max_turns,
                timeout_secs=self.timeout_secs,
                model=self.model,
                provider=self.provider,
                cwd=self.cwd,
            )

            qa_response = _parse_qa_response(
                qa_result.raw_stdout, qa_result.parsed_json
            )

            # QA timed out — treat as rejection with timeout context
            if qa_result.timed_out:
                timeout_minutes = self.timeout_secs / 60
                log.warning(
                    "qa.timeout",
                    task_label=task.label,
                    iteration=iteration,
                    context="review",
                    duration=qa_result.duration_secs,
                )
                self.ui.print_warning(
                    f"[{task.label}] QA timed out after {timeout_minutes:.0f}min during review — treating as rejection"
                )
                qa_review_timeout_entry = IterationEntry(
                    timestamp=_now_iso(),
                    iteration=self.global_iteration,
                    actor=Actor.QA,
                    task_label=task.label,
                    status=TaskStatus.ERROR,
                    payload={
                        "error": "timeout",
                        "duration": qa_result.duration_secs,
                    },
                    raw_output=qa_result.raw_stderr[:500],
                )
                self.log.append(qa_review_timeout_entry)
                self.ui.add_iteration(qa_review_timeout_entry)
                qa_response = QAResponse(
                    decision="reject",
                    feedback=_timeout_feedback("QA", self.timeout_secs),
                    concerns=["QA agent timed out during review"],
                )

            # QA returned unparsable output — treat as rejection with raw feedback
            if qa_response is None:
                self.ui.print_warning(
                    f"[{task.label}] QA did not return valid JSON, treating as rejection."
                )
                qa_response = QAResponse(
                    decision="reject",
                    feedback=qa_result.raw_stdout[:300]
                    if qa_result.raw_stdout
                    else "QA returned no structured response",
                    concerns=["QA did not return structured JSON"],
                )

            # Log QA iteration
            qa_status = (
                TaskStatus.APPROVED
                if qa_response.decision == "approve"
                else TaskStatus.FEEDBACK
            )
            qa_entry = IterationEntry(
                timestamp=_now_iso(),
                iteration=self.global_iteration,
                actor=Actor.QA,
                task_label=task.label,
                status=qa_status,
                payload=qa_response.to_dict(),
                raw_output=qa_result.raw_stdout[:500],
            )
            self.log.append(qa_entry)
            self.ui.add_iteration(qa_entry)

            if qa_response.decision == "approve":
                log.info(
                    "qa.approved",
                    task_label=task.label,
                    iteration=iteration,
                    feedback=qa_response.feedback[:100],
                )
                self.ui.print_success(
                    f"[{task.label}] ✓ APPROVED by QA: {qa_response.feedback[:100]}"
                )
                self._finalize_task(phase, task)
                return

            elif qa_response.decision == "needs_user_input":
                resolved = self._interactive_chat_loop(
                    task=task,
                    phase=phase,
                    qa_response=qa_response,
                )
                if not resolved:
                    # User skipped — mark done and move on
                    self._finalize_task(phase, task)
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
                log.info(
                    "qa.rejected",
                    task_label=task.label,
                    iteration=iteration,
                    feedback=qa_response.feedback[:200],
                    num_concerns=len(qa_response.concerns),
                )
                self.ui.print_warning(
                    f"[{task.label}] ✗ REJECTED by QA: {qa_response.feedback[:200]}"
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

        # Max iterations reached — mark done to prevent infinite loop
        log.error(
            "feedback_loop.max_iterations",
            task_label=task.label,
            max_iterations=self.max_iterations,
        )
        self.ui.print_error(
            f"[{task.label}] Max iterations ({self.max_iterations}) reached. "
            f"Marking task done and moving on."
        )
        self._finalize_task(phase, task)
        max_iter_entry = IterationEntry(
            timestamp=_now_iso(),
            iteration=self.global_iteration,
            actor=Actor.QA,
            task_label=task.label,
            status=TaskStatus.ERROR,
            payload={"error": "max_iterations_reached"},
        )
        self.log.append(max_iter_entry)
        self.ui.add_iteration(max_iter_entry)
