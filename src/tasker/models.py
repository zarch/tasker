"""Data models for the tasker pipeline."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ── Task & Phase models ────────────────────────────────────────────

@dataclass
class Task:
    """A single actionable item inside a phase."""
    phase_index: int
    task_index: int
    text: str
    done: bool = False

    # Sub-phase context — set by parser when the task sits under a ### heading
    subphase: str = ""
    subphase_index: int = -1  # 0-based task index within its ### group (-1 if no subphase)

    # VCS tracking — set by the VCS backend (jj or git) when enabled
    base_ref: str | None = None    # parent ref (what we diff against): jj change ID or git commit hash
    task_ref: str | None = None    # task ref: jj change ID or git branch name

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


class TaskStatus(str, enum.Enum):
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    FEEDBACK = "feedback"
    APPROVED = "approved"
    ERROR = "error"
    BLOCKED = "blocked"
    NEEDS_USER_INPUT = "needs_user_input"


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
    """Escalation stages when goose returns malformed output."""
    NORMAL = "normal"           # first attempt, no special instruction
    CONTINUE = "continue"       # "continue from where you left off"
    SUBTASK = "subtask"         # "break into subtasks and implement one at a time"
    SUMMARIZE = "summarize"     # "summarize progress and difficulties"

    @property
    def max_attempts(self) -> int:
        return 3


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
            "feedback": self.feedback or "",
            "recovery_instruction": self.recovery_instruction or "",
        }
        return params


@dataclass
class DevResponse:
    """Dev → QA: result of implementation."""
    status: str  # "done" | "blocked"
    summary: str
    files_modified: list[str]
    notes: str = ""
    blocker_description: str = ""  # what is blocking (when status="blocked")
    blocker_suggestion: str = ""   # dev's suggestion to resolve (when status="blocked")

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
