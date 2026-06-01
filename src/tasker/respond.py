"""Structured JSON output helper for goose agents.

Usage from inside a goose agent session (PYTHONPATH is pre-injected):

    python3 -m tasker.respond dev done --summary "Implemented X" --files src/foo.py
    python3 -m tasker.respond dev blocked --summary "Stuck on Y" --blocker "Missing spec"
    python3 -m tasker.respond dev started --summary "Starting task"
    python3 -m tasker.respond qa approve --feedback "LGTM"
    python3 -m tasker.respond qa reject --feedback "Fix X" --concerns "bug" "typo"
    python3 -m tasker.respond qa needs_user_input --feedback "Need clarification" --question "Which API?"
    python3 -m tasker.respond decompose yes --reason "Too large" --subtasks 'P1.T3.1|Do A' 'P1.T3.2|Do B'
    python3 -m tasker.respond arch retry --reason "Transient failure"

The output is a single JSON line printed to stdout, wrapped in a code block
so the orchestrator's cascade extraction picks it up.  All arguments are
validated through Pydantic models — impossible to produce malformed JSON.
"""

from __future__ import annotations

import argparse
import json


def _emit(obj: dict) -> None:
    """Print the JSON wrapped in a markdown code block for cascade extraction."""
    text = json.dumps(obj, ensure_ascii=False)
    print(f"```json\n{text}\n```")


def cmd_dev(args: argparse.Namespace) -> None:
    from tasker.schema import DevResponse

    resp = DevResponse(
        status=args.status,
        summary=args.summary,
        files_modified=args.files or [],
        notes=args.notes or "",
        blocker_description=args.blocker or "",
        blocker_suggestion=args.blocker_suggestion or "",
    )
    _emit(resp.model_dump())


def cmd_qa(args: argparse.Namespace) -> None:
    from tasker.schema import QAResponse

    resp = QAResponse(
        decision=args.decision,
        feedback=args.feedback,
        concerns=args.concerns or [],
        user_question=args.question or "",
    )
    _emit(resp.model_dump())


def cmd_decompose(args: argparse.Namespace) -> None:
    from tasker.schema import DecomposeResponse, SubtaskSchema

    subtasks = []
    if args.subtasks:
        for raw in args.subtasks:
            parts = raw.split("|", 1)
            label = parts[0].strip()
            text = parts[1].strip() if len(parts) > 1 else label
            subtasks.append(SubtaskSchema(label=label, text=text))

    resp = DecomposeResponse(
        should_decompose=args.should_decompose in ("yes", "true", "1"),
        reason=args.reason or "",
        subtasks=subtasks,
    )
    _emit(resp.model_dump())


def cmd_arch(args: argparse.Namespace) -> None:
    from tasker.schema import ArchResponse

    resp = ArchResponse(
        action=args.action,
        reason=args.reason,
    )
    _emit(resp.model_dump())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tasker.respond",
        description="Emit validated JSON for the tasker orchestrator. "
        "Run as: python3 -m tasker.respond <role> <verb> [options]",
    )
    subparsers = parser.add_subparsers(dest="role", required=True)

    # ── dev ──────────────────────────────────────────────────────
    dev_p = subparsers.add_parser("dev", help="Dev agent response")
    dev_p.add_argument(
        "status",
        choices=["done", "blocked", "started"],
        help="Response status",
    )
    dev_p.add_argument(
        "--summary", required=True, help="What was implemented or attempted"
    )
    dev_p.add_argument("--files", nargs="*", help="List of modified file paths")
    dev_p.add_argument("--notes", default="", help="Additional context")
    dev_p.add_argument(
        "--blocker", default="", help="Blocker description (when status=blocked)"
    )
    dev_p.add_argument(
        "--blocker-suggestion", default="", help="Suggested resolution for blocker"
    )
    dev_p.set_defaults(func=cmd_dev)

    # ── qa ───────────────────────────────────────────────────────
    qa_p = subparsers.add_parser("qa", help="QA agent response")
    qa_p.add_argument(
        "decision",
        choices=["approve", "reject", "needs_user_input"],
        help="Review decision",
    )
    qa_p.add_argument("--feedback", required=True, help="Explanation of the decision")
    qa_p.add_argument("--concerns", nargs="*", help="Specific issues found")
    qa_p.add_argument(
        "--question",
        default="",
        help="Question for the user (when decision=needs_user_input)",
    )
    qa_p.set_defaults(func=cmd_qa)

    # ── decompose ────────────────────────────────────────────────
    dec_p = subparsers.add_parser("decompose", help="Task decomposition response")
    dec_p.add_argument(
        "should_decompose",
        choices=["yes", "no"],
        help="Whether to split the task",
    )
    dec_p.add_argument("--reason", default="", help="Why decompose or not")
    dec_p.add_argument(
        "--subtasks",
        nargs="*",
        help='Subtask definitions as "LABEL|TEXT" pairs',
    )
    dec_p.set_defaults(func=cmd_decompose)

    # ── arch ─────────────────────────────────────────────────────
    arch_p = subparsers.add_parser("arch", help="Architect agent response")
    arch_p.add_argument(
        "action",
        choices=["recompose", "clarify", "skip", "retry"],
        help="Action to take",
    )
    arch_p.add_argument("--reason", required=True, help="Why this action")
    arch_p.set_defaults(func=cmd_arch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
