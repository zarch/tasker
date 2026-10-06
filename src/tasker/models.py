"""Data models for the tasker pipeline."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


# ── Task & Phase models ────────────────────────────────────────────


@dataclass
class Task:
    """A single actionable item inside a phase."""

    phase_index: int
    task_index: int
    text: str
    done: bool = False
    skipped: bool = False  # True when max_iterations was reached without QA approval
    failed: bool = False  # True when persistently failed (written as [~] in markdown)

    # Sub-phase context — set by parser when the task sits under a ### heading
    subphase: str = ""
    subphase_index: int = (
        -1
    )  # 0-based task index within its ### group (-1 if no subphase)

    # VCS tracking — set by the VCS backend (jj or git) when enabled
    base_ref: str | None = (
        None  # parent ref (what we diff against): jj change ID or git commit hash
    )
    task_ref: str | None = None  # task ref: jj change ID or git branch name

    # Legacy aliases (deprecated — use base_ref / task_ref)
    @property
    def base_change_id(self) -> str | None:
        return self.base_ref

    @base_change_id.setter
    def base_change_id(self, value: str | None) -> None:
        self.base_ref = value

    @property
    def task_change_id(self) -> str | None:
        return self.task_ref

    @task_change_id.setter
    def task_change_id(self, value: str | None) -> None:
        self.task_ref = value

    @property
    def label(self) -> str:
        if self.subphase and self.subphase_index >= 0:
            # Derive short key from subphase heading (e.g. "P1-2 API Endpoints" → "P1-2")
            short = self.subphase.split()[0] if self.subphase else ""
            return f"{short}.T{self.subphase_index + 1}"
        return f"P{self.phase_index + 1}.T{self.task_index + 1}"

    @property
    def jj_description(self) -> str:
        """Generate a commit message from the task label and text.

        Deprecated — use vcs_description instead.
        """
        return self.vcs_description

    @property
    def vcs_description(self) -> str:
        """Generate a VCS commit message from the task label and text."""
        return f"{self.label}: {self.text}"


@dataclass
class Phase:
    """A group of tasks (e.g. "Phase 1 — MVP").

    A Phase may optionally have a `subphase` identifier when the task file
    uses ### headings to group tasks within a phase (e.g. "### P1-1 Core").
    The `subphase` value is the full ### heading text (without the hashes).
    """

    index: int
    title: str
    tasks: list[Task] = field(default_factory=list)
    subphase: str = ""  # non-empty when this phase group comes from a ### heading

    @property
    def total(self) -> int:
        return len(self.tasks)

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.done)

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.completed == self.total


# ── Actor / status enums ──────────────────────────────────────────


class Actor(str, enum.Enum):
    QA = "qa"
    DEV = "dev"
    ARCH = "arch"
    SYSTEM = "system"


class TaskStatus(str, enum.Enum):
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    FEEDBACK = "feedback"
    APPROVED = "approved"
    ERROR = "error"
    BLOCKED = "blocked"
    NEEDS_USER_INPUT = "needs_user_input"
    SESSION_START = "session_start"


# ── Recovery state for graceful degradation ──────────────────────


class SessionScope(str, enum.Enum):
    """Controls when goose sessions are rotated (new session = fresh context).

    phase    — one session per ## Phase heading (coarsest, most context)
    subphase — one session per ### sub-heading (default, good balance)
    task     — one session per task (finest, least context but no overflow)
    """

    PHASE = "phase"
    SUBPHASE = "subphase"
    TASK = "task"


class RecoveryStage(str, enum.Enum):
    """Escalation stages when the developer agent returns malformed output."""

    NORMAL = "normal"  # first attempt, no special instruction
    CONTINUE = "continue"  # "continue from where you left off"
    SUBTASK = "subtask"  # "break into subtasks and implement one at a time"
    SUMMARIZE = "summarize"  # "summarize progress and difficulties"
    RESTART = "restart"  # "fresh session, start over from scratch"

    @property
    def max_attempts(self) -> int:
        return 1 if self == RecoveryStage.RESTART else 3


class QARecoveryStage(str, enum.Enum):
    """Escalation stages when the QA agent returns malformed output.

    Mirrors RecoveryStage but without SUBTASK (QA doesn't split work into
    subtasks — its failure mode is producing overly long reviews without
    the required JSON decision block).
    """

    NORMAL = "normal"  # first attempt, no special instruction
    CONTINUE = "continue"  # "keep review focused, output the JSON block"
    SUMMARIZE = "summarize"  # "stop investigating, just output JSON"
    RESTART = "restart"  # "fresh QA session, review from scratch"

    @property
    def max_attempts(self) -> int:
        return 1 if self == QARecoveryStage.RESTART else 3


# -- Rate-limit / connection-error resilience ---------------------


@dataclass
class FallbackModel:
    """Configuration for a fallback model to use when the primary provider
    is unreachable (e.g. rate-limited).  The fallback is per-role: dev tasks
    can use a local model (cheaper, good enough for coding) while QA tasks
    use a strong cloud model (reviews need higher quality).

    The fallback is tried once per goose call — the next orchestrator turn
    goes back to the primary model.
    """

    provider: str  # e.g. "ollama", "claude-code"
    model: str  # e.g. "qwen3.5:9b", "sonnet"
    max_attempts: int = 2  # how many times to try the fallback before giving up
    timeout_secs: int | None = None  # None → same timeout as the primary call


# goose provider that drives the local `claude` CLI (Claude subscription,
# no API key).  Default backend for the automatic fallback and escalation.
CLAUDE_CODE_PROVIDER = "claude-code"
CLAUDE_CODE_DEFAULT_MODEL = "sonnet"

# Escalated calls run the hardest tasks on a slower, stronger model.
ESCALATION_DEFAULT_TIMEOUT_SECS = 1800


@dataclass
class EscalationConfig:
    """Stronger model used once a task is flagged as stuck.

    Unlike :class:`FallbackModel` (provider unreachable, one call), the
    escalation is sticky: from the moment the stuckness check fires, every
    call of an escalated role for that task uses this model, until the
    orchestrator moves on to another task.  The ARCH role, which only runs
    for stuck tasks, always uses it when listed in *roles*.
    """

    provider: str  # e.g. "claude-code"
    model: str  # e.g. "opus"
    roles: frozenset[Actor] = frozenset({Actor.ARCH, Actor.DEV})
    timeout_secs: int = ESCALATION_DEFAULT_TIMEOUT_SECS  # replaces --timeout


@dataclass
class RateLimitConfig:
    """Controls exponential backoff when the goose subprocess fails with
    a connection / rate-limit error (e.g. "Error: not connected").
    The tasker detects transient connection failures and waits an
    exponentially growing delay before retrying instead of burning
    through the recovery-stage budget.

    Attributes:
        enabled:            Master switch.  False disables backoff entirely.
        base_delay_secs:    First retry delay (doubles each attempt).
        max_delay_secs:     Hard ceiling on any single backoff delay.
        max_retries:        Max consecutive connection-error retries before
                            giving up and letting recovery handle it.
        jitter:             Fractional jitter (0-1) to avoid thundering herd.
        fallback_dev:       Fallback model for dev role (e.g. local ollama).
        fallback_qa:        Fallback model for QA role (e.g. cloud claude).
    """

    enabled: bool = True
    base_delay_secs: float = 30.0
    max_delay_secs: float = 300.0
    max_retries: int = 5
    jitter: float = 0.25
    fallback_dev: FallbackModel | None = None
    fallback_qa: FallbackModel | None = None

    def next_delay(self, attempt: int) -> float:
        """Compute the backoff delay for *attempt* (1-based).

        Uses capped exponential backoff with jitter.
        """
        import random

        ceiling = min(self.base_delay_secs * (2 ** (attempt - 1)), self.max_delay_secs)
        return random.uniform(ceiling * (1 - self.jitter), ceiling)

    @classmethod
    def disabled(cls) -> "RateLimitConfig":
        """Return a config that skips all backoff logic."""
        return cls(enabled=False)


# ── JSONL iteration log entry ─────────────────────────────────────


@dataclass
class IterationEntry:
    """One QA↔Dev exchange recorded in the JSONL log."""

    timestamp: str
    iteration: int
    actor: Actor
    task_label: str
    status: TaskStatus
    payload: dict[str, Any] | None = None
    raw_output: str | None = None
    evidence: dict[str, Any] | None = (
        None  # run post-mortem (return_code, turns, retries...)
    )
    checkpoint: bool = False
    json_blocks_found: int = 0
    json_blocks_cascade: bool = False
    assistant_turns: int = 0
    total_turns: int = 0
    output_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "timestamp": self.timestamp,
            "iteration": self.iteration,
            "actor": self.actor.value,
            "task_label": self.task_label,
            "status": self.status.value,
        }
        if self.payload is not None:
            d["payload"] = self.payload
        if self.raw_output is not None:
            d["raw_output"] = self.raw_output
        if self.evidence is not None:
            d["evidence"] = self.evidence
        if self.checkpoint:
            d["checkpoint"] = True
        if self.json_blocks_found:
            d["json_blocks_found"] = self.json_blocks_found
        if self.json_blocks_cascade:
            d["json_blocks_cascade"] = True
        if self.assistant_turns:
            d["assistant_turns"] = self.assistant_turns
        if self.total_turns:
            d["total_turns"] = self.total_turns
        if self.output_chars:
            d["output_chars"] = self.output_chars
        return d


# ── Payload schemas for QA ↔ Dev communication ────────────────────


@dataclass
class DevRequest:
    """QA → Dev: implement this task."""

    task_label: str
    task_text: str
    qa_session_id: str
    dev_session_id: str
    iteration: int
    max_turns: int = 100
    feedback: str | None = None  # non-None on re-work
    recovery_instruction: str | None = None  # non-None during degradation

    def to_params(self) -> dict[str, str]:
        """Return key-value pairs for `goose run --params KEY=VALUE`."""
        params: dict[str, str] = {
            "task_label": self.task_label,
            "task_text": self.task_text,
            "qa_session_id": self.qa_session_id,
            "dev_session_id": self.dev_session_id,
            "iteration": str(self.iteration),
            "max_turns": str(self.max_turns),
            "feedback": self.feedback or "",
            "recovery_instruction": self.recovery_instruction or "",
        }
        return params


@dataclass
class DevResponse:
    """Dev → QA: result of implementation."""

    status: str  # "done" | "blocked" | "started"
    summary: str
    files_modified: list[str]
    notes: str = ""
    blocker_description: str = ""  # what is blocking (when status="blocked")
    blocker_suggestion: str = ""  # dev's suggestion to resolve (when status="blocked")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "summary": self.summary,
            "files_modified": self.files_modified,
            "notes": self.notes,
        }
        if self.blocker_description:
            d["blocker_description"] = self.blocker_description
        if self.blocker_suggestion:
            d["blocker_suggestion"] = self.blocker_suggestion
        return d


@dataclass
class QARequest:
    """Orchestrator → QA: review this dev work."""

    task_label: str
    task_text: str
    dev_response: DevResponse
    dev_session_id: str
    qa_session_id: str
    iteration: int
    project_context: str = ""
    dev_blocked: bool = False  # True when dev returned status="blocked"
    blocker_description: str = ""  # copied from DevResponse when blocked
    max_turns: int = 100

    def to_params(self) -> dict[str, str]:
        """Return key-value pairs for `goose run --params KEY=VALUE`."""
        params: dict[str, str] = {
            "task_label": self.task_label,
            "task_text": self.task_text,
            "dev_summary": self.dev_response.summary,
            "files_modified": ", ".join(self.dev_response.files_modified),
            "dev_notes": self.dev_response.notes,
            "dev_session_id": self.dev_session_id,
            "qa_session_id": self.qa_session_id,
            "iteration": str(self.iteration),
            # Always provide all declared recipe params (goose validates)
            "dev_blocked": "true" if self.dev_blocked else "false",
            "blocker_description": self.blocker_description or "",
            "blocker_suggestion": self.dev_response.blocker_suggestion or "",
            # Chat-mode params (empty when not in chat mode)
            "user_message": "",
            "conversation_history": "",
            # JJ diff context (empty when jj is not enabled)
            "project_context": self.project_context or "",
            "max_turns": str(self.max_turns),
        }
        return params


@dataclass
class QAResponse:
    """QA → Orchestrator: approve, reject, or request user input."""

    decision: str  # "approve" | "reject" | "needs_user_input"
    feedback: str
    concerns: list[str] = field(default_factory=list)
    user_question: str = ""  # question to ask the user (when needs_user_input)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "decision": self.decision,
            "feedback": self.feedback,
        }
        if self.concerns:
            d["concerns"] = self.concerns
        if self.user_question:
            d["user_question"] = self.user_question
        return d


@dataclass
class UserChatRequest:
    """Orchestrator → QA (chat mode): relay user's answer."""

    task_label: str
    task_text: str
    blocker_description: str
    user_message: str
    conversation_history: str  # accumulated user↔QA transcript
    qa_session_id: str
    dev_session_id: str

    def to_params(self) -> dict[str, str]:
        """Return key-value pairs for `goose run --params KEY=VALUE`."""
        return {
            "task_label": self.task_label,
            "task_text": self.task_text,
            "blocker_description": self.blocker_description,
            "user_message": self.user_message,
            "conversation_history": self.conversation_history,
            "qa_session_id": self.qa_session_id,
            "dev_session_id": self.dev_session_id,
            # Always provide all declared QA recipe params (goose validates)
            "dev_summary": "",
            "files_modified": "",
            "dev_notes": "",
            "iteration": "0",
            "dev_blocked": "true" if self.blocker_description else "false",
            "blocker_suggestion": "",
        }


# ── Task decomposition ────────────────────────────────────────────


@dataclass
class Subtask:
    """A single piece of work extracted from a larger task by QA decomposition."""

    label: str  # e.g. "P1.T3.1" or "P1.T3 — Part A"
    text: str  # focused, self-contained description


@dataclass
class DecomposeResponse:
    """QA → Orchestrator: task decomposition result.

    The QA agent either decides the task is small enough to pass through
    as-is (``should_decompose=False``) or returns a list of focused subtasks.
    """

    should_decompose: bool
    reason: str  # brief explanation of the decision
    subtasks: list[Subtask] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "should_decompose": self.should_decompose,
            "reason": self.reason,
        }
        if self.subtasks:
            d["subtasks"] = [{"label": s.label, "text": s.text} for s in self.subtasks]
        return d


# ── Architect (ARCH) agent communication ──────────────────────────


class ArchAction(str, enum.Enum):
    """Actions the ARCH agent can take when a task is stuck."""

    REDECOMPOSE = "recompose"  # split into smaller subtasks
    CLARIFY = "clarify"  # rewrite the task text with better instructions
    SKIP = "skip"  # mark task as not needed
    RETRY = "retry"  # give the dev agent another chance


@dataclass
class ArchRequest:
    """Orchestrator → ARCH: review a stuck task and decide what to do."""

    task_label: str
    task_text: str
    error_summary: str  # summary of failures (from _build_error_summary)
    code_state: str  # summary of what exists on disk (from _build_code_state_summary)

    def to_params(self) -> dict[str, str]:
        return {
            "task_label": self.task_label,
            "task_text": self.task_text,
            "error_summary": self.error_summary,
            "code_state": self.code_state,
        }


@dataclass
class ArchResponse:
    """ARCH → Orchestrator: decision on how to unblock a stuck task."""

    action: str  # ArchAction value
    reason: str
    subtasks: list[Subtask] = field(default_factory=list)  # for REDECOMPOSE
    new_task_text: str = ""  # for CLARIFY
    max_iterations_override: int | None = None  # for RETRY

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "action": self.action,
            "reason": self.reason,
        }
        if self.subtasks:
            d["subtasks"] = [{"label": s.label, "text": s.text} for s in self.subtasks]
        if self.new_task_text:
            d["new_task_text"] = self.new_task_text
        if self.max_iterations_override is not None:
            d["max_iterations_override"] = self.max_iterations_override
        return d
