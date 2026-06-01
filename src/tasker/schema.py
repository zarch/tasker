"""Pydantic models for structured agent output.

Importable by goose agents via PYTHONPATH injection:

    from tasker.schema import DevResponse, QAResponse
    print(DevResponse(status="done", summary="...", files_modified=[]).model_dump_json())
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SubtaskSchema(BaseModel):
    """A single piece of work extracted from a larger task."""

    label: str = Field(
        description="Short identifier, e.g. 'P1.T3.1' or 'P1.T3 — Part A'"
    )
    text: str = Field(description="Focused, self-contained description of the subtask")


class DevResponse(BaseModel):
    """Dev agent structured response."""

    status: Literal["done", "blocked", "started"] = Field(
        description='"done" when the task is complete, "blocked" when unable to proceed, '
        '"started" for the initial checkpoint (analytics-only — never triggers blocked handling)'
    )
    summary: str = Field(description="What was implemented or attempted")
    files_modified: list[str] = Field(
        default_factory=list,
        description="List of file paths that were created or modified",
    )
    notes: str = Field(default="", description="Additional context or caveats")
    blocker_description: str = Field(
        default="", description="What is preventing progress (when status='blocked')"
    )
    blocker_suggestion: str = Field(
        default="",
        description="Suggested resolution for the blocker (when status='blocked')",
    )


class QAResponse(BaseModel):
    """QA agent structured response."""

    decision: Literal["approve", "reject", "needs_user_input"] = Field(
        description='"approve" to accept, "reject" to send back to dev, "needs_user_input" to ask a question'
    )
    feedback: str = Field(description="Explanation of the decision")
    concerns: list[str] = Field(
        default_factory=list, description="Specific issues found during review"
    )
    user_question: str = Field(
        default="",
        description="Question to ask the user (when decision='needs_user_input')",
    )


class DecomposeResponse(BaseModel):
    """QA decomposition result: split a task into focused subtasks."""

    should_decompose: bool = Field(
        description="True if the task should be split into subtasks, False to leave as-is"
    )
    reason: str = Field(description="Brief explanation of the decomposition decision")
    subtasks: list[SubtaskSchema] = Field(
        default_factory=list,
        description="Subtask definitions (when should_decompose=True)",
    )


class ArchResponse(BaseModel):
    """ARCH agent decision on how to unblock a stuck task."""

    action: Literal["recompose", "clarify", "skip", "retry"] = Field(
        description="The action the orchestrator should take to resolve stuckness"
    )
    reason: str = Field(description="Why this action was chosen")
    subtasks: list[SubtaskSchema] = Field(
        default_factory=list,
        description="New subtask definitions (when action='recompose')",
    )
    new_task_text: str = Field(
        default="", description="Rewritten task description (when action='clarify')"
    )
    max_iterations_override: int | None = Field(
        default=None,
        description="Override max iterations for the next dev attempt (when action='retry')",
    )
