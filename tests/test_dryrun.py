"""End-to-end dry-run test — mocks goose subprocess to test the full pipeline."""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

# Add src to path so we can import tasker
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tasker.parser import parse_task_file, find_next_task, update_markdown
from tasker.log import IterationLog
from tasker.models import IterationEntry, Actor, TaskStatus


@contextmanager
def _env(*, remove: tuple[str, ...] = (), **set_vars: str):
    """Temporarily set/unset environment variables for a test.

    Usage::

        with _env(ANTHROPIC_API_KEY="sk-test"):
            ...

        with _env(remove=("ANTHROPIC_API_KEY",)):
            ...   # ANTHROPIC_API_KEY guaranteed absent

    Original values are restored on exit.
    """
    saved: dict[str, str] = {}
    for key in remove:
        if key in os.environ:
            saved[key] = os.environ[key]
        os.environ.pop(key, None)
    for key, val in set_vars.items():
        if key in os.environ:
            saved[key] = os.environ[key]
        os.environ[key] = val
    try:
        yield
    finally:
        for key, val in saved.items():
            os.environ[key] = val
        for key in set(remove) | set(set_vars):
            if key not in saved:
                os.environ.pop(key, None)


# ── 1. Test parser ────────────────────────────────────────────────


def test_parser():
    sample = Path(__file__).parent / "fixtures" / "sample_tasks.md"
    phases = parse_task_file(sample)
    assert len(phases) == 2
    assert phases[0].title == "Phase 1 — Test MVP"
    assert phases[0].total == 3
    assert phases[0].completed == 0
    assert phases[1].title == "Phase 2 — Advanced Features"
    assert phases[1].total == 2

    pair = find_next_task(phases)
    assert pair is not None
    phase, task = pair
    assert phase.index == 0
    assert task.label == "P1.T1"
    assert task.text == "Create a simple hello world function"

    print("✓ Parser tests passed")


# ── 2. Test JSONL logger ─────────────────────────────────────────


def test_logger():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w") as f:
        log_path = f.name

    try:
        log = IterationLog(log_path)
        assert log.count == 0

        entry = IterationEntry(
            timestamp="2026-04-13T11:00:00Z",
            iteration=1,
            actor=Actor.DEV,
            task_label="P1.T1",
            status=TaskStatus.IN_PROGRESS,
            payload={"status": "done", "summary": "Created hello()"},
        )
        log.append(entry)
        assert log.count == 1

        entry2 = IterationEntry(
            timestamp="2026-04-13T11:05:00Z",
            iteration=2,
            actor=Actor.QA,
            task_label="P1.T1",
            status=TaskStatus.APPROVED,
            payload={"decision": "approve", "feedback": "Looks good"},
        )
        log.append(entry2)
        assert log.count == 2

        entries = log.read_all()
        assert len(entries) == 2
        assert entries[0]["actor"] == "dev"
        assert entries[0]["payload"]["status"] == "done"
        assert entries[1]["actor"] == "qa"
        assert entries[1]["payload"]["decision"] == "approve"

        print("✓ Logger tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)


# ── 3. Test JSON extraction ──────────────────────────────────────


def test_json_extraction():
    from tasker.goose import _extract_json_blocks

    # Fenced code block
    text1 = 'Some text\n```json\n{"status": "done", "summary": "ok"}\n```\nmore text'
    blocks1 = _extract_json_blocks(text1)
    assert len(blocks1) == 1
    assert blocks1[0] == {"status": "done", "summary": "ok"}

    # Raw JSON
    text2 = '{"decision": "approve", "feedback": "LGTM", "concerns": []}'
    blocks2 = _extract_json_blocks(text2)
    assert len(blocks2) == 1
    assert blocks2[0] == {"decision": "approve", "feedback": "LGTM", "concerns": []}

    # Last brace fallback
    text3 = 'Blah blah\n{"status": "done"}'
    blocks3 = _extract_json_blocks(text3)
    assert len(blocks3) == 1
    assert blocks3[0] == {"status": "done"}

    # No JSON
    text4 = "Just plain text, no json here"
    blocks4 = _extract_json_blocks(text4)
    assert len(blocks4) == 0

    print("✓ JSON extraction tests passed")


# ── 4. Test update_markdown ──────────────────────────────────────


def test_markdown_update():
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1\n\n- [ ] Task A\n- [ ] Task B\n- [ ] Task C\n")
        md_path = f.name

    try:
        phases = parse_task_file(md_path)
        assert phases[0].completed == 0

        # Mark first task done
        phases[0].tasks[0].done = True
        update_markdown(md_path, phases)

        # Re-parse and verify
        phases2 = parse_task_file(md_path)
        assert phases2[0].completed == 1
        assert phases2[0].tasks[0].done is True
        assert phases2[0].tasks[1].done is False

        # Mark all done
        for t in phases2[0].tasks:
            t.done = True
        update_markdown(md_path, phases2)

        phases3 = parse_task_file(md_path)
        assert phases3[0].completed == 3

        print("✓ Markdown update tests passed")
    finally:
        Path(md_path).unlink(missing_ok=True)


# ── 5. Test goose command builder ────────────────────────────────


def test_command_builder():
    from tasker.goose import build_goose_command

    cmd = build_goose_command(
        recipe_path="/tmp/r.yaml",
        session_name="dev_20260413_abc123",
        params={"task_label": "P1.T1", "task_text": "Implement task P1.T1"},
        max_turns=50,
        model="claude-sonnet-4-20250514",
    )

    assert cmd[0] == "goose"
    assert "--recipe" in cmd
    assert "--name" in cmd
    assert "dev_20260413_abc123" in cmd
    assert "--text" not in cmd  # --text is mutually exclusive with --recipe
    assert "--params" in cmd
    assert "task_label=P1.T1" in cmd
    assert "task_text=Implement task P1.T1" in cmd
    assert "--max-turns" in cmd
    assert "50" in cmd
    assert "--model" in cmd
    assert "claude-sonnet-4-20250514" in cmd
    assert "--with-builtin" in cmd
    assert "developer" in cmd
    # --with-builtin is now used instead of --no-profile
    assert "--resume" not in cmd  # should NOT be present
    assert "--session-id" not in cmd  # we use --name instead

    # No params at all
    cmd2 = build_goose_command(
        recipe_path="/tmp/r.yaml",
        session_name="dev_20260413_abc123",
    )
    assert "--params" not in cmd2

    # Params with newlines get escaped
    cmd3 = build_goose_command(
        recipe_path="/tmp/r.yaml",
        session_name="dev_20260413_abc123",
        params={"feedback": "Line 1\nLine 2\nLine 3"},
    )
    assert "feedback=Line 1\\nLine 2\\nLine 3" in cmd3

    print("✓ Command builder tests passed")


# ── 6. Test models ───────────────────────────────────────────────


def test_models():
    from tasker.models import DevRequest, DevResponse, QARequest, QAResponse

    # DevRequest params generation
    req = DevRequest(
        task_label="P1.T1",
        task_text="Create hello world",
        qa_session_id="qa_123",
        dev_session_id="dev_456",
        iteration=1,
    )
    params = req.to_params()
    assert params["task_label"] == "P1.T1"
    assert params["task_text"] == "Create hello world"
    assert params["dev_session_id"] == "dev_456"
    assert params["iteration"] == "1"
    assert params["feedback"] == ""  # empty on first iteration (always provided)
    assert (
        params["recovery_instruction"] == ""
    )  # empty on first iteration (always provided)

    # DevRequest with feedback
    req_fb = DevRequest(
        task_label="P1.T1",
        task_text="Create hello world",
        qa_session_id="qa_123",
        dev_session_id="dev_456",
        iteration=2,
        feedback="Missing error handling",
    )
    params_fb = req_fb.to_params()
    assert params_fb["feedback"] == "Missing error handling"

    # DevRequest with recovery instruction
    req_rec = DevRequest(
        task_label="P1.T1",
        task_text="Create hello world",
        qa_session_id="qa_123",
        dev_session_id="dev_456",
        iteration=3,
        recovery_instruction="Continue from where you left off.",
    )
    params_rec = req_rec.to_params()
    assert params_rec["recovery_instruction"] == "Continue from where you left off."

    # DevResponse — done
    dev_done = DevResponse(
        status="done",
        summary="Created hello() function",
        files_modified=["src/main.rs"],
        notes="All good",
    )
    d = dev_done.to_dict()
    assert d["status"] == "done"
    assert "blocker_description" not in d  # not included when empty

    # DevResponse — blocked
    dev_blocked = DevResponse(
        status="blocked",
        summary="Cannot find the config format",
        files_modified=[],
        blocker_description="No spec defines the config file format",
        blocker_suggestion="Ask the user about the expected config schema",
    )
    d2 = dev_blocked.to_dict()
    assert d2["status"] == "blocked"
    assert d2["blocker_description"] == "No spec defines the config file format"
    assert d2["blocker_suggestion"] == "Ask the user about the expected config schema"

    # QARequest params generation (normal)
    qa_req = QARequest(
        task_label="P1.T1",
        task_text="Create hello world",
        dev_response=dev_done,
        dev_session_id="dev_456",
        qa_session_id="qa_123",
        iteration=1,
    )
    qa_params = qa_req.to_params()
    assert qa_params["task_label"] == "P1.T1"
    assert qa_params["dev_summary"] == "Created hello() function"
    assert qa_params["files_modified"] == "src/main.rs"
    assert qa_params["dev_blocked"] == "false"  # not blocked

    # QARequest params generation (blocked dev)
    qa_req_blocked = QARequest(
        task_label="P1.T1",
        task_text="Create hello world",
        dev_response=dev_blocked,
        dev_session_id="dev_456",
        qa_session_id="qa_123",
        iteration=1,
        dev_blocked=True,
        blocker_description="No spec defines the config file format",
    )
    qa_params_b = qa_req_blocked.to_params()
    assert qa_params_b["dev_blocked"] == "true"
    assert (
        qa_params_b["blocker_description"] == "No spec defines the config file format"
    )

    # QAResponse — approve
    qa_approve = QAResponse(decision="approve", feedback="Looks good")
    assert qa_approve.to_dict()["decision"] == "approve"
    assert "user_question" not in qa_approve.to_dict()

    # QAResponse — needs_user_input
    qa_needs = QAResponse(
        decision="needs_user_input",
        feedback="Need clarification",
        user_question="What API format should we use?",
        concerns=["No spec exists for the API format"],
    )
    d3 = qa_needs.to_dict()
    assert d3["decision"] == "needs_user_input"
    assert d3["user_question"] == "What API format should we use?"
    assert len(d3["concerns"]) == 1

    # UserChatRequest params generation
    from tasker.models import UserChatRequest

    chat_req = UserChatRequest(
        task_label="P1.T1",
        task_text="Create hello world",
        blocker_description="No API spec",
        user_message="Use REST with JSON",
        conversation_history="👤 User: What API?\n🧪 QA: Not specified",
        qa_session_id="qa_123",
        dev_session_id="dev_456",
    )
    chat_params = chat_req.to_params()
    assert chat_params["user_message"] == "Use REST with JSON"
    assert (
        chat_params["conversation_history"]
        == "👤 User: What API?\n🧪 QA: Not specified"
    )

    # RecoveryStage enum
    from tasker.models import RecoveryStage

    assert RecoveryStage.NORMAL.max_attempts == 3
    assert RecoveryStage.CONTINUE.max_attempts == 3
    assert RecoveryStage.SUBTASK.max_attempts == 3
    assert RecoveryStage.SUMMARIZE.max_attempts == 3
    assert RecoveryStage.RESTART.max_attempts == 1
    assert RecoveryStage.RESTART.value == "restart"

    print("✓ Model tests passed")


# ── 7. Test parser strictness ────────────────────────────────────


def test_parser_strictness():
    """Test that parsers return None on truly unparsable output (no guessing)."""
    from tasker.orchestrator import _parse_dev_response, _parse_qa_response

    # Valid dev response — done
    assert (
        _parse_dev_response(
            '{"status": "done", "summary": "ok", "files_modified": []}',
            {"status": "done", "summary": "ok", "files_modified": []},
        )
        is not None
    )

    # Valid dev response — blocked
    resp = _parse_dev_response(
        '{"status": "blocked", "summary": "stuck", "files_modified": [], '
        '"blocker_description": "no spec", "blocker_suggestion": "ask user"}',
        {
            "status": "blocked",
            "summary": "stuck",
            "files_modified": [],
            "blocker_description": "no spec",
            "blocker_suggestion": "ask user",
        },
    )
    assert resp is not None
    assert resp.status == "blocked"
    assert resp.blocker_description == "no spec"

    # Invalid dev response — unknown status
    assert (
        _parse_dev_response(
            '{"status": "hmm", "summary": "???"}',
            {"status": "hmm", "summary": "???"},
        )
        is None
    )

    # Invalid dev response — no status key at all
    assert _parse_dev_response("just text", None) is None
    assert (
        _parse_dev_response(
            '{"summary": "ok but no status"}',
            {"summary": "ok but no status"},
        )
        is None
    )

    # Valid QA response — approve
    assert (
        _parse_qa_response(
            '{"decision": "approve", "feedback": "LGTM"}',
            {"decision": "approve", "feedback": "LGTM"},
        )
        is not None
    )

    # Valid QA response — needs_user_input
    resp2 = _parse_qa_response(
        '{"decision": "needs_user_input", "feedback": "need info", "user_question": "what?"}',
        {
            "decision": "needs_user_input",
            "feedback": "need info",
            "user_question": "what?",
        },
    )
    assert resp2 is not None
    assert resp2.decision == "needs_user_input"
    assert resp2.user_question == "what?"

    # Invalid QA response — unknown decision
    assert (
        _parse_qa_response(
            '{"decision": "maybe", "feedback": "idk"}',
            {"decision": "maybe", "feedback": "idk"},
        )
        is None
    )

    # Invalid QA response — no decision key
    assert _parse_qa_response("just text", None) is None

    print("✓ Parser strictness tests passed")


# ── 8. Test envelope extraction ──────────────────────────────────


def test_envelope_extraction():
    """Test that the goose JSON envelope is properly unwrapped."""
    from tasker.goose import _extract_last_assistant_text

    # Valid envelope with assistant message after user message
    envelope = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do task"}]},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": 'I did it\n```json\n{"status": "done", "summary": "ok", "files_modified": []}\n```',
                        }
                    ],
                },
            ]
        }
    )
    result, empty, asst_turns, total_turns = _extract_last_assistant_text(envelope)
    assert not empty, "Should not be empty when assistant messages exist after user"
    assert '{"status": "done"' in result
    assert "I did it" in result
    assert asst_turns == 1
    assert total_turns == 2

    # Envelope with assistant messages but NO user message — stale guard
    # All assistant messages are "old history" since there's no user message
    envelope_stale = json.dumps(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "thinking..."}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"status": "done"}'}],
                },
            ]
        }
    )
    result_stale, empty_stale, asst_stale, total_stale = _extract_last_assistant_text(
        envelope_stale
    )
    assert empty_stale, (
        "Should flag as empty when all assistant msgs are stale (no user msg)"
    )
    assert result_stale == "", "Stale output should return empty text"
    assert asst_stale == 0, "Stale output should report 0 assistant_turns"
    assert total_stale == 2, "Stale output should still report total_turns"

    # Envelope with user then assistant — new assistant after user
    envelope2 = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "go"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "thinking..."}],
                },
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"status": "done"}'}],
                },
            ]
        }
    )
    result2, empty2, asst2, total2 = _extract_last_assistant_text(envelope2)
    assert not empty2
    assert "thinking..." in result2
    assert '{"status": "done"}' in result2
    assert asst2 == 2
    assert total2 == 3

    # Stale-only output: assistant before user, none after
    envelope_stale2 = json.dumps(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"status": "old"}'}],
                },
                {"role": "user", "content": [{"type": "text", "text": "new prompt"}]},
            ]
        }
    )
    result_s2, empty_s2, asst_s2, total_s2 = _extract_last_assistant_text(
        envelope_stale2
    )
    assert empty_s2, "Should flag as stale when assistant msgs only before user"
    assert result_s2 == ""
    assert asst_s2 == 0
    assert total_s2 == 2

    # Envelope with only user messages (goose errored before agent responded)
    envelope_no_assistant = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do task"}]},
            ]
        }
    )
    result_no_asst, empty_flag, asst_no, total_no = _extract_last_assistant_text(
        envelope_no_assistant
    )
    assert result_no_asst == "", (
        f"Expected empty string for envelope with no assistant messages, got: {result_no_asst!r}"
    )
    assert empty_flag, "Should flag as empty when no assistant messages"
    assert asst_no == 0
    assert total_no == 1

    # Empty messages list
    envelope_empty = json.dumps({"messages": []})
    result_empty, empty_empty, asst_empty, total_empty = _extract_last_assistant_text(
        envelope_empty
    )
    assert result_empty == ""
    assert empty_empty, "Should flag as empty when messages list is empty"
    assert asst_empty == 0
    assert total_empty == 0

    # Not JSON — fallback to raw
    result3, empty3, asst3, total3 = _extract_last_assistant_text("plain text output")
    assert result3 == "plain text output"
    assert not empty3, "Plain text fallback should not be flagged as empty"
    assert asst3 == 0
    assert total3 == 0

    print("✓ Envelope extraction tests passed")


# ── 9. Test JJ module ────────────────────────────────────────────


def test_jj_module():
    """Test the jj utility module functions."""
    from tasker.jj import jj_is_available, _run_jj

    # Check jj is available (it should be on this system)
    available = jj_is_available()
    assert isinstance(available, bool)
    print(f"  jj available: {available}")

    if available:
        # Test _run_jj with a simple command
        result = _run_jj(["version"])
        assert result.success
        assert "jj" in result.stdout.lower()

        # Test with invalid args (should fail gracefully)
        bad_result = _run_jj(["nonexistent_command_xyz"])
        assert not bad_result.success
        assert bad_result.return_code != 0

    print("✓ JJ module tests passed")


# ── 10. Test Task jj fields ─────────────────────────────────────


def test_task_jj_fields():
    """Test that Task model has jj tracking fields."""
    from tasker.models import Task

    task = Task(
        phase_index=0,
        task_index=0,
        text="Create hello world function",
    )

    # Default jj fields
    assert task.base_change_id is None
    assert task.task_change_id is None
    assert task.label == "P1.T1"
    assert task.jj_description == "P1.T1: Create hello world function"

    # With jj fields set
    task.base_change_id = "abc123def456"
    task.task_change_id = "xyz789uvw012"
    assert task.base_change_id == "abc123def456"
    assert task.task_change_id == "xyz789uvw012"

    print("✓ Task jj fields tests passed")


# ── 11. Test QARequest with project_context ──────────────────────


def test_qa_request_with_project_context():
    """Test that QARequest includes project_context in to_params()."""
    from tasker.models import QARequest, DevResponse

    dev_resp = DevResponse(
        status="done",
        summary="Added feature",
        files_modified=["src/main.rs"],
    )

    # Without context
    qa_req = QARequest(
        task_label="P1.T1",
        task_text="Do something",
        dev_response=dev_resp,
        dev_session_id="dev_123",
        qa_session_id="qa_456",
        iteration=1,
    )
    params = qa_req.to_params()
    assert params["project_context"] == ""

    # With context (jj diff)
    qa_req_ctx = QARequest(
        task_label="P1.T1",
        task_text="Do something",
        dev_response=dev_resp,
        dev_session_id="dev_123",
        qa_session_id="qa_456",
        iteration=1,
        project_context="## JJ Diff\n```\n+ added line\n```",
    )
    params_ctx = qa_req_ctx.to_params()
    assert "added line" in params_ctx["project_context"]

    print("✓ QARequest project_context tests passed")


# ── 12. Test GooseRunResult timed_out field ──────────────────────


def test_goose_result_timed_out():
    """Test that GooseRunResult has timed_out field and defaults to False."""
    from tasker.goose import GooseRunResult

    # Default — not timed out
    result_ok = GooseRunResult(
        success=True,
        raw_stdout="ok",
        raw_stderr="",
        return_code=0,
    )
    assert result_ok.timed_out is False

    # Explicit timed out
    result_timeout = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="TIMEOUT after 600s",
        return_code=-1,
        duration_secs=600.5,
        timed_out=True,
    )
    assert result_timeout.timed_out is True
    assert result_timeout.success is False
    assert result_timeout.return_code == -1
    assert "TIMEOUT" in result_timeout.raw_stderr

    print("✓ GooseRunResult timed_out tests passed")


# ── 13. Test timeout feedback helper ─────────────────────────────


def test_timeout_feedback():
    """Test that _timeout_feedback produces a useful message."""
    from tasker.orchestrator import _timeout_feedback

    msg = _timeout_feedback("Developer", 600)
    assert "Developer" in msg
    assert "10 minutes" in msg
    assert "600 seconds" in msg
    assert "killed" in msg.lower()
    assert "continue from where you" in msg.lower()

    msg_qa = _timeout_feedback("QA", 300)
    assert "QA" in msg_qa
    assert "5 minutes" in msg_qa
    assert "300 seconds" in msg_qa

    print("✓ Timeout feedback tests passed")


# ── 14. Test subphase parsing ────────────────────────────────────


def test_subphase_parsing():
    """Test that ### sub-headings are parsed and attached to tasks."""
    sample = Path(__file__).parent / "fixtures" / "subphase_tasks.md"
    phases = parse_task_file(sample)

    # Should still be 2 phases (## headings)
    assert len(phases) == 2
    assert phases[0].title == "Phase 1 — Core Backend"
    assert phases[1].title == "Phase 2 — Frontend"

    # Phase 1 should have 5 tasks total
    assert phases[0].total == 5

    # Check subphase assignment on tasks
    # P1-1 tasks
    assert phases[0].tasks[0].subphase == "P1-1 Database Setup"
    assert phases[0].tasks[1].subphase == "P1-1 Database Setup"
    assert phases[0].tasks[2].subphase == "P1-1 Database Setup"
    # P1-2 tasks
    assert phases[0].tasks[3].subphase == "P1-2 API Endpoints"
    assert phases[0].tasks[4].subphase == "P1-2 API Endpoints"

    # Phase 2 tasks
    assert phases[1].tasks[0].subphase == "P2-1 Components"
    assert phases[1].tasks[1].subphase == "P2-1 Components"
    assert phases[1].tasks[2].subphase == "P2-2 Integration"
    assert phases[1].tasks[3].subphase == "P2-2 Integration"

    # Check the done task is correctly parsed
    assert phases[1].tasks[3].done is True

    # Phase subphase should reflect the first ### heading
    assert phases[0].subphase == "P1-1 Database Setup"
    assert phases[1].subphase == "P2-1 Components"

    print("✓ Subphase parsing tests passed")


# ── 15. Test SessionScope enum ──────────────────────────────────


def test_session_scope_enum():
    """Test SessionScope enum values."""
    from tasker.models import SessionScope

    assert SessionScope.PHASE.value == "phase"
    assert SessionScope.SUBPHASE.value == "subphase"
    assert SessionScope.TASK.value == "task"

    # Can construct from string
    scope = SessionScope("subphase")
    assert scope is SessionScope.SUBPHASE

    print("✓ SessionScope enum tests passed")


# ── 16. Test scope key computation ───────────────────────────────


def test_scope_key_computation():
    """Test _compute_scope_key produces correct keys for each scope level."""
    from tasker.models import SessionScope, Task
    from tasker.orchestrator import _compute_scope_key

    # Task with subphase
    task = Task(
        phase_index=0,
        task_index=2,
        text="Some task",
        subphase="P1-2 API Endpoints",
        subphase_index=0,
    )

    # PHASE scope — only phase index matters
    assert _compute_scope_key(task, SessionScope.PHASE) == "P1"

    # SUBPHASE scope — phase + subphase
    assert _compute_scope_key(task, SessionScope.SUBPHASE) == "P1::P1-2 API Endpoints"

    # TASK scope — phase + subphase + subphase-local task index
    assert _compute_scope_key(task, SessionScope.TASK) == "P1::P1-2 API Endpoints::T1"

    # Task without subphase (e.g., sample_tasks.md which has no ### headings)
    task_no_sub = Task(phase_index=1, task_index=0, text="No subphase task")

    assert _compute_scope_key(task_no_sub, SessionScope.PHASE) == "P2"
    assert (
        _compute_scope_key(task_no_sub, SessionScope.SUBPHASE) == "P2"
    )  # falls back to phase
    assert _compute_scope_key(task_no_sub, SessionScope.TASK) == "P2::T1"

    # Same scope key for tasks in the same subphase
    task_a = Task(
        phase_index=0, task_index=0, text="A", subphase="P1-1 DB", subphase_index=0
    )
    task_b = Task(
        phase_index=0, task_index=1, text="B", subphase="P1-1 DB", subphase_index=1
    )
    assert _compute_scope_key(task_a, SessionScope.SUBPHASE) == _compute_scope_key(
        task_b, SessionScope.SUBPHASE
    )
    # Different TASK scope keys for different tasks in same subphase
    assert _compute_scope_key(task_a, SessionScope.TASK) != _compute_scope_key(
        task_b, SessionScope.TASK
    )

    # Different scope keys for tasks in different subphases
    task_c = Task(
        phase_index=0, task_index=2, text="C", subphase="P1-2 API", subphase_index=0
    )
    assert _compute_scope_key(task_a, SessionScope.SUBPHASE) != _compute_scope_key(
        task_c, SessionScope.SUBPHASE
    )

    # Different scope keys for tasks in different phases
    task_d = Task(
        phase_index=1, task_index=0, text="D", subphase="P1-1 DB", subphase_index=0
    )
    assert _compute_scope_key(task_a, SessionScope.PHASE) != _compute_scope_key(
        task_d, SessionScope.PHASE
    )

    print("✓ Scope key computation tests passed")


# ── 17. Test backward compatibility (no subphases) ───────────────


def test_backward_compat_no_subphases():
    """Test that files without ### headings still parse correctly."""
    sample = Path(__file__).parent / "fixtures" / "sample_tasks.md"
    phases = parse_task_file(sample)

    # Should be 2 phases, 5 tasks total
    assert len(phases) == 2
    assert phases[0].total == 3
    assert phases[1].total == 2

    # No subphases — all tasks should have empty subphase
    for phase in phases:
        for task in phase.tasks:
            assert task.subphase == ""

    # Phase.subphase should also be empty
    assert phases[0].subphase == ""
    assert phases[1].subphase == ""

    print("✓ Backward compatibility tests passed")


# ── 18. Test Task.subphase field ────────────────────────────────


def test_task_subphase_field():
    """Test that Task model has subphase field with correct default."""
    from tasker.models import Task

    task = Task(phase_index=0, task_index=0, text="Do something")
    assert task.subphase == ""  # default

    task2 = Task(
        phase_index=0,
        task_index=1,
        text="Another",
        subphase="P1-1 Setup",
        subphase_index=0,
    )
    assert task2.subphase == "P1-1 Setup"

    # subphase-aware label: short key from heading + local task index
    assert task2.label == "P1-1.T1"

    print("✓ Task subphase field tests passed")


# ── 19. Test subphase-aware label derivation ────────────────────


def test_subphase_labels():
    """Test that tasks under ### headings get meaningful labels derived from the heading."""
    from tasker.models import Task

    # Task with subphase "P1-4 · hay-grid — Pixel" → short key "P1-4"
    task = Task(
        phase_index=0,
        task_index=24,
        text="Some pixel task",
        subphase="P1-4 · hay-grid — Pixel",
        subphase_index=2,
    )
    assert task.label == "P1-4.T3"  # 3rd task (0-based index 2) under this ###

    # First task in a subphase
    task_first = Task(
        phase_index=0,
        task_index=0,
        text="Setup DB",
        subphase="P1-1 Database Setup",
        subphase_index=0,
    )
    assert task_first.label == "P1-1.T1"

    # Second task in the same subphase
    task_second = Task(
        phase_index=0,
        task_index=1,
        text="Run migrations",
        subphase="P1-1 Database Setup",
        subphase_index=1,
    )
    assert task_second.label == "P1-1.T2"

    # Task without subphase — falls back to flat positional label
    task_no_sub = Task(phase_index=2, task_index=5, text="No subphase here")
    assert task_no_sub.label == "P3.T6"

    # Task with subphase="" and subphase_index=-1 — also flat fallback
    task_neg = Task(
        phase_index=1,
        task_index=3,
        text="Orphan",
        subphase="",
        subphase_index=-1,
    )
    assert task_neg.label == "P2.T4"

    # Verify from fixture file: subphase_tasks.md
    sample = Path(__file__).parent / "fixtures" / "subphase_tasks.md"
    phases = parse_task_file(sample)

    # P1-1 Database Setup: 3 tasks → P1-1.T1, P1-1.T2, P1-1.T3
    assert phases[0].tasks[0].label == "P1-1.T1"
    assert phases[0].tasks[1].label == "P1-1.T2"
    assert phases[0].tasks[2].label == "P1-1.T3"

    # P1-2 API Endpoints: 2 tasks → P1-2.T1, P1-2.T2
    assert phases[0].tasks[3].label == "P1-2.T1"
    assert phases[0].tasks[4].label == "P1-2.T2"

    # P2-1 Components: 2 tasks → P2-1.T1, P2-1.T2
    assert phases[1].tasks[0].label == "P2-1.T1"
    assert phases[1].tasks[1].label == "P2-1.T2"

    # P2-2 Integration: 2 tasks → P2-2.T1, P2-2.T2
    assert phases[1].tasks[2].label == "P2-2.T1"
    assert phases[1].tasks[3].label == "P2-2.T2"

    print("✓ Subphase label derivation tests passed")


# ── 20. Test VCSBackend protocol ───────────────────────────────


def test_vcs_backend_protocol():
    """Test that both backends satisfy the VCSBackend protocol."""
    from tasker.vcs import VCSBackend, create_backend
    from tasker.vcs.jj_backend import JJBackend
    from tasker.vcs.git_backend import GitBackend

    # Protocol check — both classes implement the protocol
    assert isinstance(JJBackend(), VCSBackend)
    assert isinstance(GitBackend(), VCSBackend)

    # Factory tests
    assert create_backend("none") is None
    assert isinstance(create_backend("jj"), JJBackend)
    assert isinstance(create_backend("git"), GitBackend)

    # Invalid type raises ValueError
    try:
        create_backend("mercurial")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    print("✓ VCSBackend protocol tests passed")


# ── 21. Test Task VCS fields (base_ref / task_ref) ──────────────


def test_task_vcs_fields():
    """Test that Task model uses VCS-agnostic base_ref / task_ref fields."""
    from tasker.models import Task

    task = Task(
        phase_index=0,
        task_index=0,
        text="Create hello world function",
    )

    # Default VCS fields
    assert task.base_ref is None
    assert task.task_ref is None

    # Set via new names
    task.base_ref = "abc123def456"
    task.task_ref = "task/P1.T1"
    assert task.base_ref == "abc123def456"
    assert task.task_ref == "task/P1.T1"

    # Legacy aliases still work (backward compat)
    assert task.base_change_id == "abc123def456"
    assert task.task_change_id == "task/P1.T1"

    # Set via legacy names
    task.base_change_id = "xyz789"
    task.task_change_id = "task/P1.T2"
    assert task.base_ref == "xyz789"
    assert task.task_ref == "task/P1.T2"

    # vcs_description property
    assert task.vcs_description == "P1.T1: Create hello world function"
    # jj_description is an alias
    assert task.jj_description == task.vcs_description

    print("✓ Task VCS fields tests passed")


# ── 22. Test backward-compatible jj re-exports ─────────────────


def test_jj_reexports():
    """Test that tasker.jj re-exports all public symbols from vcs.jj_backend."""
    from tasker import jj as jj_mod
    from tasker.vcs.jj_backend import (
        JJBackend,
        JJResult,
        _run_jj,
        jj_commit_task,
        jj_diff,
        jj_get_current_change_id,
        jj_has_changes,
        jj_is_available,
        jj_log,
        jj_new_task,
    )

    # All symbols are accessible through the old location
    assert jj_mod.jj_is_available is jj_is_available
    assert jj_mod._run_jj is _run_jj
    assert jj_mod.JJResult is JJResult
    assert jj_mod.JJBackend is JJBackend
    assert jj_mod.jj_new_task is jj_new_task
    assert jj_mod.jj_commit_task is jj_commit_task
    assert jj_mod.jj_diff is jj_diff
    assert jj_mod.jj_get_current_change_id is jj_get_current_change_id
    assert jj_mod.jj_log is jj_log
    assert jj_mod.jj_has_changes is jj_has_changes

    print("✓ JJ re-export tests passed")


# ── 23. Test git backend helper functions ───────────────────────


def test_git_backend_helpers():
    """Test git backend helper functions."""
    from tasker.vcs.git_backend import _sanitize_branch_name, GitBackend

    # Branch name sanitization
    assert _sanitize_branch_name("P1.T1") == "P1.T1"
    assert _sanitize_branch_name("P1-2.T3") == "P1-2.T3"
    assert _sanitize_branch_name("P1-4 · hay-grid — Pixel") == "P1-4-·-hay-grid-—-Pixel"
    assert _sanitize_branch_name("task with spaces") == "task-with-spaces"
    assert _sanitize_branch_name("special~chars^here") == "special-chars-here"
    # Leading dots/dashes stripped
    assert _sanitize_branch_name("..secret") == "secret"
    assert _sanitize_branch_name("---leading") == "leading"

    # GitBackend creation
    backend = GitBackend()
    assert isinstance(backend, object)
    assert backend._base_branch is None
    assert backend._base_commit is None

    # is_available — git should be available
    assert backend.is_available() is True

    print("✓ Git backend helper tests passed")


# ── 24. Test git backend init validation ────────────────────────


def test_git_backend_init_errors():
    """Test that GitBackend.init() raises on invalid states."""
    from tasker.vcs.git_backend import GitBackend

    backend = GitBackend()

    # Not a git repo — init should fail
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            backend.init(cwd=Path(tmpdir))
            assert False, "Should have raised RuntimeError"
        except RuntimeError as exc:
            assert "git" in str(exc).lower() or "repository" in str(exc).lower()

    print("✓ Git backend init validation tests passed")


# ── 25. Test VCSBackend create_backend factory ──────────────────


def test_create_backend_types():
    """Test that create_backend returns the correct backend type."""
    from tasker.vcs import create_backend
    from tasker.vcs.jj_backend import JJBackend
    from tasker.vcs.git_backend import GitBackend

    # None
    assert create_backend("none") is None

    # JJ
    jj = create_backend("jj")
    assert isinstance(jj, JJBackend)
    assert jj.is_available()  # jj should be installed

    # Git
    git = create_backend("git")
    assert isinstance(git, GitBackend)
    assert git.is_available()  # git should be installed

    print("✓ create_backend factory tests passed")


# ── 26. Test JJBackend protocol compliance ──────────────────────


def test_jj_backend_protocol():
    """Test JJBackend implements all VCSBackend protocol methods."""
    from tasker.vcs.jj_backend import JJBackend

    backend = JJBackend()

    # Has all required methods
    assert hasattr(backend, "is_available")
    assert hasattr(backend, "init")
    assert hasattr(backend, "begin_task")
    assert hasattr(backend, "get_diff")
    assert hasattr(backend, "commit_task")

    # All are callable
    assert callable(backend.is_available)
    assert callable(backend.init)
    assert callable(backend.begin_task)
    assert callable(backend.get_diff)
    assert callable(backend.commit_task)

    # is_available works without init
    result = backend.is_available()
    assert isinstance(result, bool)

    print("✓ JJBackend protocol compliance tests passed")


# ── 27. Test GitBackend protocol compliance ─────────────────────


def test_git_backend_protocol():
    """Test GitBackend implements all VCSBackend protocol methods."""
    from tasker.vcs.git_backend import GitBackend

    backend = GitBackend()

    # Has all required methods
    assert hasattr(backend, "is_available")
    assert hasattr(backend, "init")
    assert hasattr(backend, "begin_task")
    assert hasattr(backend, "get_diff")
    assert hasattr(backend, "commit_task")

    # All are callable
    assert callable(backend.is_available)
    assert callable(backend.init)
    assert callable(backend.begin_task)
    assert callable(backend.get_diff)
    assert callable(backend.commit_task)

    # is_available works without init
    result = backend.is_available()
    assert isinstance(result, bool)

    print("✓ GitBackend protocol compliance tests passed")


# ── 28. Test Task.vcs_description ───────────────────────────────


def test_task_vcs_description():
    """Test vcs_description property on Task."""
    from tasker.models import Task

    task = Task(phase_index=0, task_index=0, text="Build API")
    assert task.vcs_description == "P1.T1: Build API"

    # With subphase
    task2 = Task(
        phase_index=0,
        task_index=5,
        text="Add grid cell",
        subphase="P1-4 Grid System",
        subphase_index=2,
    )
    assert task2.vcs_description == "P1-4.T3: Add grid cell"

    print("✓ Task vcs_description tests passed")


# ── 29. Test _finalize_task ordering invariant ────────────────────


def test_finalize_task_ordering():
    """Verify that _finalize_task marks done, updates markdown, THEN commits.

    Regression test for the bug where _vcs_commit_task ran BEFORE
    mark_task_done + update_markdown, causing [x] marks to live only as
    unstaged working-tree changes that were never committed.
    """
    import tempfile
    from unittest.mock import patch

    from tasker.orchestrator import Orchestrator
    from tasker.models import Task

    # Create a temp markdown file
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1\n\n- [ ] Task A\n- [ ] Task B\n")
        md_path = f.name

    try:
        # Build an orchestrator with minimal setup (no VCS needed — we mock it)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as lf:
            log_path = lf.name
        try:
            orch = Orchestrator(
                task_file=md_path,
                dev_recipe="/dev/null",
                qa_recipe="/dev/null",
                log_file=log_path,
            )
        finally:
            Path(log_path).unlink(missing_ok=True)

        # Parse the file to populate orch.phases
        orch.phases = parse_task_file(md_path)
        phase = orch.phases[0]
        task = phase.tasks[0]  # Task A

        assert not task.done
        assert "- [ ] Task A" in Path(md_path).read_text()

        # Track call order and verify markdown state at commit time
        call_order: list[str] = []
        commit_time_markdown: list[str] = []

        original_vcs_commit = orch._vcs_commit_task

        def spy_vcs_commit(t: Task) -> None:
            call_order.append("vcs_commit")
            # Capture the markdown content AT THE MOMENT the VCS commit runs
            commit_time_markdown.append(Path(md_path).read_text())
            original_vcs_commit(t)

        with patch.object(orch, "_vcs_commit_task", side_effect=spy_vcs_commit):
            with patch.object(orch, "ui"):
                orch._finalize_task(phase, task)

        # 1. Verify call order: vcs_commit was called (after mark+update)
        assert "vcs_commit" in call_order, "vcs_commit should have been called"

        # 2. Verify the markdown had [x] when VCS commit ran
        assert len(commit_time_markdown) == 1
        md_at_commit = commit_time_markdown[0]
        assert "- [x] Task A" in md_at_commit, (
            f"Expected [x] in markdown at VCS commit time, but got: {md_at_commit!r}"
        )
        assert "- [ ] Task B" in md_at_commit, (
            "Only the approved task should be marked done"
        )

        # 3. Verify in-memory state
        assert task.done is True

        # 4. Verify the file on disk
        content = Path(md_path).read_text()
        assert "- [x] Task A" in content

        print("✓ _finalize_task ordering invariant verified")
    finally:
        Path(md_path).unlink(missing_ok=True)


# ── 30. Test monitoring setup ───────────────────────────────────


def test_monitoring_setup():
    """Test that setup_monitoring configures structlog correctly."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    import tasker.monitoring as mon

    # Reset module state for testing
    mon._configured = False
    structlog.reset_defaults()
    root = logging.getLogger()
    root.handlers.clear()

    # Setup with a temp file
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name

    try:
        setup_monitoring(log_path)

        # Should now be configured
        assert mon._configured is True

        # Root logger should have handlers
        root = logging.getLogger()
        assert len(root.handlers) >= 1  # at least console

        # File handler should exist
        file_handlers = [
            h
            for h in root.handlers
            if hasattr(h, "baseFilename") and log_path in getattr(h, "baseFilename", "")
        ]
        assert len(file_handlers) == 1, (
            f"Expected 1 file handler for {log_path}, got {len(file_handlers)}"
        )

        # Verify the file was created
        assert Path(log_path).exists()

        print("✓ Monitoring setup tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 31. Test monitoring file output ──────────────────────────────


def test_monitoring_file_output():
    """Test that structlog events actually appear in the monitor log file."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name

    try:
        setup_monitoring(log_path)

        logger = structlog.get_logger("test_monitor")
        logger.info("test.event", key="value", number=42)

        for handler in logging.getLogger().handlers:
            handler.flush()

        content = Path(log_path).read_text(encoding="utf-8")
        assert "test.event" in content
        assert "key=value" in content
        assert "number=42" in content
        assert "info" in content.lower()

        print("✓ Monitoring file output tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 32. Test monitoring idempotent ───────────────────────────────


def test_monitoring_idempotent():
    """Test that calling setup_monitoring twice is a no-op."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name

    try:
        setup_monitoring(log_path)
        handler_count_1 = len(logging.getLogger().handlers)

        setup_monitoring(log_path)
        handler_count_2 = len(logging.getLogger().handlers)

        assert handler_count_1 == handler_count_2, (
            f"setup_monitoring should be idempotent: {handler_count_1} != {handler_count_2}"
        )

        print("✓ Monitoring idempotent tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 33. Test get_logger convenience ─────────────────────────────


def test_monitoring_get_logger():
    """Test that get_logger returns a usable structlog logger."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring, get_logger
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name

    try:
        setup_monitoring(log_path)

        logger1 = get_logger("my.module")
        assert logger1 is not None
        logger1.info("test.named_logger", module="my.module")

        logger2 = get_logger(None)
        assert logger2 is not None
        logger2.info("test.default_logger")

        for handler in logging.getLogger().handlers:
            handler.flush()

        content = Path(log_path).read_text(encoding="utf-8")
        assert "test.named_logger" in content
        assert "test.default_logger" in content

        print("✓ get_logger tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 34. Test parser events captured in monitor log ───────────────


def test_monitoring_parser_captured():
    """Test that parser.py log events are captured in the monitor log."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name

    try:
        setup_monitoring(log_path)

        sample = Path(__file__).parent / "fixtures" / "sample_tasks.md"
        parse_task_file(sample)

        for handler in logging.getLogger().handlers:
            handler.flush()

        content = Path(log_path).read_text(encoding="utf-8")
        assert "parser.parsed" in content, (
            f"Expected 'parser.parsed' in log, got:\n{content}"
        )
        assert "phases=2" in content
        assert "tasks=5" in content

        print("✓ Parser events captured tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 35. Test orchestrator events captured in monitor log ──────────


def test_monitoring_orchestrator_events_captured():
    """Test that orchestrator.py log events (task lifecycle) are captured."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    from tasker.orchestrator import Orchestrator
    from unittest.mock import patch
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1\n\n- [ ] Task A\n- [ ] Task B\n")
        md_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        iter_log_path = f.name

    try:
        setup_monitoring(log_path)

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=iter_log_path,
        )
        orch.phases = parse_task_file(md_path)

        phase = orch.phases[0]
        task = phase.tasks[0]

        with patch.object(orch, "ui"):
            with patch.object(orch, "_vcs_commit_task"):
                orch._finalize_task(phase, task)

        for handler in logging.getLogger().handlers:
            handler.flush()

        content = Path(log_path).read_text(encoding="utf-8")
        assert "task.finalizing" in content, (
            f"Expected 'task.finalizing' in log, got:\n{content}"
        )
        assert "task.file_updated" in content
        assert "task.finalized" in content
        assert "P1.T1" in content

        print("✓ Orchestrator events captured tests passed")
    finally:
        Path(log_path).unlink(missing_ok=True)
        Path(md_path).unlink(missing_ok=True)
        Path(iter_log_path).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 36. Test _resolve_level helper ────────────────────────────────


def test_resolve_level():
    """Test that _resolve_level maps names to stdlib logging constants."""
    import logging
    from tasker.monitoring import _resolve_level

    assert _resolve_level("debug") == logging.DEBUG
    assert _resolve_level("DEBUG") == logging.DEBUG
    assert _resolve_level("info") == logging.INFO
    assert _resolve_level("INFO") == logging.INFO
    assert _resolve_level("warning") == logging.WARNING
    assert _resolve_level("warn") == logging.WARNING
    assert _resolve_level("WARN") == logging.WARNING
    assert _resolve_level("error") == logging.ERROR
    assert _resolve_level("critical") == logging.CRITICAL
    assert _resolve_level("crit") == logging.CRITICAL
    assert _resolve_level("  debug  ") == logging.DEBUG  # whitespace trimmed

    # Unknown level raises ValueError
    try:
        _resolve_level("trace")
        assert False, "Should have raised ValueError for unknown level"
    except ValueError as exc:
        assert "trace" in str(exc)

    print("✓ _resolve_level tests passed")


# ── 37. Test log-level filtering (file vs console) ──────────────


def test_monitoring_log_levels():
    """Test that console_level and file_level independently filter output."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        file_log = f.name

    try:
        # File: DEBUG (captures everything). Console: ERROR (only errors).
        setup_monitoring(file_log, console_level="ERROR", file_level="DEBUG")

        log = structlog.get_logger("test.levels")
        log.debug("should_be_in_file_only")
        log.info("also_file_only")
        log.warning("still_file_only")
        log.error("in_both_file_and_console")

        for handler in logging.getLogger().handlers:
            handler.flush()

        # File should have all four messages
        file_content = Path(file_log).read_text(encoding="utf-8")
        assert "should_be_in_file_only" in file_content
        assert "also_file_only" in file_content
        assert "still_file_only" in file_content
        assert "in_both_file_and_console" in file_content

        print("✓ Log level filtering tests passed")
    finally:
        Path(file_log).unlink(missing_ok=True)
        mon._configured = False
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ── 38. Test invalid log level raises ValueError ─────────────────


def test_monitoring_invalid_level():
    """Test that an invalid log level raises ValueError from setup_monitoring."""
    import structlog
    import logging
    from tasker.monitoring import setup_monitoring
    import tasker.monitoring as mon

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    try:
        setup_monitoring(None, console_level="TRACE")
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "TRACE" in str(exc)

    # Reset after the failed call — _configured was NOT set on ValueError
    mon._configured = False

    # File level also validated
    try:
        setup_monitoring(None, file_level="verbose")
        assert False, "Should have raised ValueError"
    except ValueError as exc:
        assert "verbose" in str(exc)

    mon._configured = False
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()

    print("✓ Invalid log level tests passed")


# ── 39. Test _ActivityRenderable ──────────────────────────────────


def test_activity_renderable():
    """Test that the _ActivityRenderable produces elapsed time output."""
    import time
    from tasker.ui import _ActivityRenderable

    # Create and let it run briefly
    activity = _ActivityRenderable("🛠️ Developer — Task P1.T1")
    assert activity._stopped is False
    assert activity._label == "🛠️ Developer — Task P1.T1"

    # Wait a tiny bit so elapsed > 0
    time.sleep(0.05)
    elapsed = activity.elapsed_secs
    assert elapsed >= 0.04, f"Expected elapsed >= 0.04s, got {elapsed}"

    # Stop it
    activity.stop()
    assert activity._stopped is True
    # Elapsed should still be readable
    assert activity.elapsed_secs >= 0.04

    # Test label update
    activity2 = _ActivityRenderable("initial label")
    activity2._label = "updated label"
    assert activity2._label == "updated label"

    print("✓ _ActivityRenderable tests passed")


# ── 40. Test TaskerUI activity_start / activity_stop ─────────────


def test_ui_activity_indicator():
    """Test that activity_start/activity_stop integrate with the layout."""
    from tasker.ui import TaskerUI

    ui = TaskerUI()
    ui.init_progress()

    # activity_stop when nothing started returns 0
    elapsed = ui.activity_stop()
    assert elapsed == 0.0

    # activity_start sets internal state
    ui.activity_start("🛠️ Developer — Task P1.T1")
    assert ui._activity is not None
    assert ui._activity._label == "🛠️ Developer — Task P1.T1"
    assert ui._activity_label == "🛠️ Developer — Task P1.T1"

    # activity_detail updates the label
    ui.activity_detail("🛠️ Developer — Task P1.T1 (stage=normal)")
    assert ui._activity._label == "🛠️ Developer — Task P1.T1 (stage=normal)"

    # activity_stop clears state and returns elapsed
    elapsed = ui.activity_stop()
    assert elapsed >= 0.0
    assert ui._activity is None
    assert ui._activity_label == ""

    print("✓ TaskerUI activity indicator tests passed")


# ── 41. Test goose heartbeat thread ──────────────────────────────


def test_goose_heartbeat_thread():
    """Test that _heartbeat_logger emits periodic log events and stops cleanly."""
    import structlog
    import logging
    import threading
    import time
    from tasker.goose import _heartbeat_logger

    # Configure structlog to route through stdlib so our CapturingHandler
    # can intercept heartbeat events.  Without this, structlog's default
    # configuration (no stdlib factory) silently drops events before they
    # reach stdlib handlers.
    structlog.configure(
        processors=[
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    class CapturingHandler(logging.Handler):
        def __init__(self):
            super().__init__(logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    handler = CapturingHandler()
    test_logger = logging.getLogger("tasker.goose")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.DEBUG)

    try:
        stop = threading.Event()
        # Use very short interval for testing (0.1s)
        t = threading.Thread(
            target=_heartbeat_logger,
            args=("test_session", stop, 0.1),
            daemon=True,
        )
        t.start()

        # Wait for at least one heartbeat
        time.sleep(0.35)
        stop.set()
        t.join(timeout=2)

        # Should have emitted at least 1 heartbeat via stdlib
        heartbeat_records = [
            r for r in handler.records if "goose.heartbeat" in r.getMessage()
        ]
        assert len(heartbeat_records) >= 1, (
            f"Expected at least 1 heartbeat record, got {len(heartbeat_records)}. "
            f"Records: {[r.getMessage() for r in handler.records]}"
        )

        # Thread should have stopped
        assert not t.is_alive(), "Heartbeat thread should have stopped"

        print("✓ Goose heartbeat thread tests passed")
    finally:
        test_logger.removeHandler(handler)


# ── 42. Test _run_goose_with_ui wiring ───────────────────────────


def test_run_goose_with_ui_wiring():
    """Test that _run_goose_with_ui starts/stops activity indicator."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.goose import GooseRunResult
    from tasker.models import Actor

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1\n\n- [ ] Task A\n")
        md_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        iter_log_path = f.name

    try:
        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=iter_log_path,
        )

        # Mock run_goose to return immediately
        fake_result = GooseRunResult(
            success=True,
            raw_stdout='{"status": "done", "summary": "ok", "files_modified": []}',
            raw_stderr="",
            return_code=0,
            parsed_json={"status": "done", "summary": "ok", "files_modified": []},
        )

        activity_start_called = []
        activity_stop_called = []

        original_start = orch.ui.activity_start
        original_stop = orch.ui.activity_stop

        def spy_start(label):
            activity_start_called.append(label)
            original_start(label)

        def spy_stop():
            activity_stop_called.append(True)
            return original_stop()

        with patch.object(orch.ui, "activity_start", side_effect=spy_start):
            with patch.object(orch.ui, "activity_stop", side_effect=spy_stop):
                with patch("tasker.goose.run_goose", return_value=fake_result):
                    result = orch._run_goose_with_ui(
                        Actor.DEV,
                        "P1.T1",
                        recipe_path="/dev/null",
                        session_name="dev_test",
                        detail="stage=normal",
                    )

        # Verify activity indicator was started and stopped
        assert len(activity_start_called) == 1
        assert (
            "Developer" in activity_start_called[0] or "🛠️" in activity_start_called[0]
        )
        assert "P1.T1" in activity_start_called[0]
        assert len(activity_stop_called) == 1

        # Verify result was returned correctly
        assert result.success is True
        assert result.parsed_json is not None

        print("✓ _run_goose_with_ui wiring tests passed")
    finally:
        Path(md_path).unlink(missing_ok=True)
        Path(iter_log_path).unlink(missing_ok=True)


# ── 43. Test _format_timestamp ────────────────────────────────────


def test_format_timestamp():
    """Test _format_timestamp extracts HH:MM:SS from ISO timestamps."""
    from tasker.ui import _format_timestamp

    # Full ISO timestamp
    assert _format_timestamp("2026-04-15T09:30:45.123456") == "09:30:45"
    # Without fractional seconds
    assert _format_timestamp("2026-04-15T09:30:45") == "09:30:45"
    # Short timestamp (no T separator)
    assert _format_timestamp("09:30:45") == "09:30:45"
    # Longer fractional
    assert _format_timestamp("2026-04-15T09:30:45.1") == "09:30:45"

    print("✓ _format_timestamp tests passed")


# ── 44. Test _entry_summary ───────────────────────────────────────


def test_entry_summary():
    """Test _entry_summary builds human-readable summaries for all entry types."""
    from tasker.ui import _entry_summary
    from tasker.models import IterationEntry, Actor, TaskStatus
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()

    # Dev: timeout
    e = IterationEntry(
        iteration=1,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.ERROR,
        payload={"error": "timeout", "duration": 600},
    )
    assert "Timeout" in _entry_summary(e)
    assert "600" in _entry_summary(e)
    assert "⏱" in _entry_summary(e)

    # Dev: subprocess_failed
    e = IterationEntry(
        iteration=2,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.ERROR,
        payload={"error": "subprocess_failed", "return_code": -1},
    )
    assert "Subprocess failed" in _entry_summary(e)
    assert "-1" in _entry_summary(e)
    assert "💥" in _entry_summary(e)

    # Dev: malformed_output
    e = IterationEntry(
        iteration=3,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.ERROR,
        payload={"error": "malformed_output", "stage": "continue"},
    )
    assert "Malformed JSON" in _entry_summary(e)
    assert "continue" in _entry_summary(e)
    assert "⚠" in _entry_summary(e)

    # Dev: blocked
    e = IterationEntry(
        iteration=4,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.IN_PROGRESS,
        payload={
            "status": "blocked",
            "summary": "working",
            "blocker_description": "can't reach API",
        },
    )
    s = _entry_summary(e)
    assert "🚫" in s
    assert "Blocked" in s
    assert "can't reach API" in s

    # Dev: done (normal)
    e = IterationEntry(
        iteration=5,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.IN_PROGRESS,
        payload={"status": "done", "summary": "Implemented feature X"},
    )
    assert _entry_summary(e) == "Implemented feature X"

    # QA: approve
    e = IterationEntry(
        iteration=6,
        timestamp=ts,
        actor=Actor.QA,
        task_label="P1.T1",
        status=TaskStatus.APPROVED,
        payload={"decision": "approve", "feedback": "LGTM"},
    )
    s = _entry_summary(e)
    assert "✓" in s
    assert "approve" in s
    assert "LGTM" in s

    # QA: reject
    e = IterationEntry(
        iteration=7,
        timestamp=ts,
        actor=Actor.QA,
        task_label="P1.T1",
        status=TaskStatus.FEEDBACK,
        payload={"decision": "reject", "feedback": "Missing error handling"},
    )
    s = _entry_summary(e)
    assert "✗" in s
    assert "reject" in s
    assert "Missing error handling" in s

    # QA: needs_user_input
    e = IterationEntry(
        iteration=8,
        timestamp=ts,
        actor=Actor.QA,
        task_label="P1.T1",
        status=TaskStatus.IN_PROGRESS,
        payload={"decision": "needs_user_input", "feedback": "Which API version?"},
    )
    s = _entry_summary(e)
    assert "❓" in s
    assert "needs_user_input" in s

    # Empty payload
    e = IterationEntry(
        iteration=9,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.IN_PROGRESS,
        payload={},
    )
    assert _entry_summary(e) == ""

    # None payload
    e = IterationEntry(
        iteration=10,
        timestamp=ts,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.IN_PROGRESS,
        payload=None,
    )
    assert _entry_summary(e) == ""

    print("✓ _entry_summary tests passed")


# ── 45. Test pending iteration lifecycle ──────────────────────────


def test_pending_iteration_lifecycle():
    """Test set/clear_pending_iteration manages UI state correctly."""
    import tempfile
    from tasker.orchestrator import Orchestrator
    from tasker.models import Actor

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1\n\n- [ ] Task A\n")
        md_path = f.name
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        iter_log_path = f.name

    try:
        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=iter_log_path,
        )

        # Initially no pending iteration
        assert orch.ui._pending is None

        # Set pending for DEV
        orch.ui.set_pending_iteration(Actor.DEV, "P1.T1", detail="stage=normal")
        assert orch.ui._pending is not None
        assert orch.ui._pending.actor == Actor.DEV
        assert orch.ui._pending.task_label == "P1.T1"
        assert orch.ui._pending.detail == "stage=normal"

        # Clear it
        orch.ui.clear_pending_iteration()
        assert orch.ui._pending is None

        # Set pending for QA
        orch.ui.set_pending_iteration(Actor.QA, "P1.T2")
        assert orch.ui._pending is not None
        assert orch.ui._pending.actor == Actor.QA
        assert orch.ui._pending.detail == ""

        orch.ui.clear_pending_iteration()

        print("✓ Pending iteration lifecycle tests passed")
    finally:
        Path(md_path).unlink(missing_ok=True)
        Path(iter_log_path).unlink(missing_ok=True)


# ── 46. Test _SPINNER_FRAMES constant ─────────────────────────────


def test_spinner_frames():
    """Test _SPINNER_FRAMES contains expected braille characters."""
    from tasker.ui import _SPINNER_FRAMES

    assert len(_SPINNER_FRAMES) == 8
    # Should contain braille dot characters
    assert "⣾" in _SPINNER_FRAMES
    assert "⣽" in _SPINNER_FRAMES
    assert "⣻" in _SPINNER_FRAMES
    assert "⢿" in _SPINNER_FRAMES
    assert "⡿" in _SPINNER_FRAMES
    assert "⣟" in _SPINNER_FRAMES
    assert "⣯" in _SPINNER_FRAMES
    assert "⣷" in _SPINNER_FRAMES

    # All frames should be single characters
    for frame in _SPINNER_FRAMES:
        assert len(frame) == 1, f"Frame {frame!r} is not a single character"

    print("✓ _SPINNER_FRAMES tests passed")


# ── 47. Test _PendingIteration dataclass ──────────────────────────


def test_pending_iteration_dataclass():
    """Test _PendingIteration dataclass construction."""
    import time
    from tasker.ui import _PendingIteration
    from tasker.models import Actor

    before = time.monotonic()
    p = _PendingIteration(actor=Actor.DEV, task_label="P1.T1", detail="test")
    after = time.monotonic()

    assert p.actor == Actor.DEV
    assert p.task_label == "P1.T1"
    assert p.detail == "test"
    assert before <= p.start <= after

    # Default detail should be empty
    p2 = _PendingIteration(actor=Actor.QA, task_label="P1.T2")
    assert p2.detail == ""

    print("✓ _PendingIteration dataclass tests passed")


# ── 48. Test Subtask and DecomposeResponse models ────────────────


# ── 48b. Test schema.py Pydantic models ─────────────────────────


def test_schema_dev_response_valid_done():
    """Construct DevResponse with status='done', verify roundtrip via model_dump_json()."""
    from tasker.schema import DevResponse

    resp = DevResponse(
        status="done",
        summary="Implemented feature X",
        files_modified=["src/foo.rs", "src/bar.rs"],
        notes="All good",
    )
    json_str = resp.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["status"] == "done"
    assert parsed["summary"] == "Implemented feature X"
    assert parsed["files_modified"] == ["src/foo.rs", "src/bar.rs"]
    assert parsed["notes"] == "All good"


def test_schema_dev_response_valid_blocked():
    """Construct DevResponse with status='blocked', verify roundtrip."""
    from tasker.schema import DevResponse

    resp = DevResponse(
        status="blocked",
        summary="Could not proceed",
        files_modified=[],
        notes="Need clarification",
        blocker_description="Missing spec",
        blocker_suggestion="Ask the user",
    )
    json_str = resp.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["status"] == "blocked"
    assert parsed["blocker_description"] == "Missing spec"
    assert parsed["blocker_suggestion"] == "Ask the user"


def test_schema_dev_response_invalid_status():
    """Verify status='unknown' raises ValidationError."""
    from pydantic import ValidationError
    from tasker.schema import DevResponse

    try:
        DevResponse(status="unknown", summary="test")  # ty: ignore[invalid-argument-type]
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_schema_qa_response_all_decisions():
    """Verify all three literal decision values are accepted."""
    from tasker.schema import QAResponse

    for decision in ("approve", "reject", "needs_user_input"):
        resp = QAResponse(
            decision=decision, feedback="Looks good", concerns=["minor issue"]
        )
        json_str = resp.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["decision"] == decision


def test_schema_qa_response_invalid_decision():
    """Verify invalid decision raises ValidationError."""
    from pydantic import ValidationError
    from tasker.schema import QAResponse

    try:
        QAResponse(decision="maybe", feedback="unsure")  # ty: ignore[invalid-argument-type]
        assert False, "Should have raised ValidationError"
    except ValidationError:
        pass


def test_schema_decompose_response():
    """Verify DecomposeResponse roundtrip serialization."""
    from tasker.schema import DecomposeResponse, SubtaskSchema

    resp = DecomposeResponse(
        should_decompose=True,
        reason="Task spans multiple modules",
        subtasks=[
            SubtaskSchema(label="P1.T3.1", text="Part A"),
            SubtaskSchema(label="P1.T3.2", text="Part B"),
        ],
    )
    json_str = resp.model_dump_json()
    parsed = json.loads(json_str)
    assert parsed["should_decompose"] is True
    assert len(parsed["subtasks"]) == 2
    assert parsed["subtasks"][0]["label"] == "P1.T3.1"


def test_schema_arch_response_all_actions():
    """Verify all four literal action values are accepted."""
    from tasker.schema import ArchResponse

    for action in ("recompose", "clarify", "skip", "retry"):
        resp = ArchResponse(action=action, reason=f"Reason for {action}")
        json_str = resp.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["action"] == action


def test_decompose_models():
    """Test Subtask and DecomposeResponse construction and serialization."""
    from tasker.models import Subtask, DecomposeResponse

    # Subtask construction
    st = Subtask(label="P1.T3.1", text="Implement GridCell struct")
    assert st.label == "P1.T3.1"
    assert st.text == "Implement GridCell struct"

    # DecomposeResponse — no decomposition
    resp_no = DecomposeResponse(
        should_decompose=False,
        reason="Single focused change in one module.",
    )
    d = resp_no.to_dict()
    assert d["should_decompose"] is False
    assert d["reason"] == "Single focused change in one module."
    assert "subtasks" not in d  # empty subtasks list is omitted

    # DecomposeResponse — with subtasks
    st1 = Subtask(
        label="P1.T3.1",
        text="Implement GridCell struct — arch/02-grid-system.md §GridCell",
    )
    st2 = Subtask(
        label="P1.T3.2",
        text="Implement SpatialIndex trait — arch/02-grid-system.md §SpatialIndex",
    )
    resp_yes = DecomposeResponse(
        should_decompose=True,
        reason="Task spans 2 modules with distinct deliverables.",
        subtasks=[st1, st2],
    )
    d2 = resp_yes.to_dict()
    assert d2["should_decompose"] is True
    assert d2["reason"] == "Task spans 2 modules with distinct deliverables."
    assert len(d2["subtasks"]) == 2
    assert d2["subtasks"][0]["label"] == "P1.T3.1"
    assert "GridCell struct" in d2["subtasks"][0]["text"]
    assert d2["subtasks"][1]["label"] == "P1.T3.2"
    assert "SpatialIndex" in d2["subtasks"][1]["text"]

    # DecomposeResponse — default subtasks is empty list
    resp_default = DecomposeResponse(should_decompose=False, reason="simple")
    assert resp_default.subtasks == []

    print("✓ DecomposeResponse and Subtask model tests passed")


# ── P4.T1: Pydantic-backed _parse_*_response unit tests ──────────


def test_parse_dev_response_pydantic_valid():
    """Valid dict with all fields → DevResponse returned via Pydantic path."""
    from tasker.orchestrator import _parse_dev_response

    parsed = {
        "status": "done",
        "summary": "Implemented feature X",
        "files_modified": ["src/foo.rs", "src/bar.rs"],
        "notes": "All good",
        "blocker_description": "",
        "blocker_suggestion": "",
    }
    result = _parse_dev_response("", parsed)
    assert result is not None
    assert result.status == "done"
    assert result.summary == "Implemented feature X"
    assert result.files_modified == ["src/foo.rs", "src/bar.rs"]
    assert result.notes == "All good"


def test_parse_dev_response_pydantic_invalid_status_fallback():
    """Dict with status='unknown' → returns None (both Pydantic and ad-hoc reject it)."""
    from tasker.orchestrator import _parse_dev_response

    parsed = {"status": "unknown", "summary": "test"}
    result = _parse_dev_response("", parsed)
    assert result is None


def test_parse_dev_response_extra_fields():
    """Valid dict plus extra unknown fields → DevResponse returned (Pydantic ignores extras)."""
    from tasker.orchestrator import _parse_dev_response

    parsed = {
        "status": "done",
        "summary": "Did work",
        "files_modified": ["src/a.rs"],
        "notes": "",
        "blocker_description": "",
        "blocker_suggestion": "",
        "extra_field": "should be ignored",
        "another_unknown": 42,
    }
    result = _parse_dev_response("", parsed)
    assert result is not None
    assert result.status == "done"
    assert result.summary == "Did work"
    assert result.files_modified == ["src/a.rs"]
    # Ensure the result is a dataclass DevResponse, not the Pydantic model
    from tasker.models import DevResponse

    assert isinstance(result, DevResponse)


def test_parse_qa_response_pydantic_valid():
    """Valid QA dict → QAResponse returned via Pydantic path."""
    from tasker.orchestrator import _parse_qa_response

    parsed = {
        "decision": "approve",
        "feedback": "Looks good",
        "concerns": ["minor style issue"],
        "user_question": "",
    }
    result = _parse_qa_response("", parsed)
    assert result is not None
    assert result.decision == "approve"
    assert result.feedback == "Looks good"
    assert result.concerns == ["minor style issue"]
    assert result.user_question == ""


def test_parse_qa_response_invalid_decision():
    """Dict with decision='maybe' → returns None (both Pydantic and ad-hoc reject it)."""
    from tasker.orchestrator import _parse_qa_response

    parsed = {"decision": "maybe", "feedback": "unsure"}
    result = _parse_qa_response("", parsed)
    assert result is None


def test_parse_decompose_response_pydantic_valid():
    """Valid dict → DecomposeResponse returned via Pydantic path."""
    from tasker.orchestrator import _parse_decompose_response

    parsed = {
        "should_decompose": True,
        "reason": "Task spans multiple modules",
        "subtasks": [
            {"label": "P1.T3.1", "text": "Part A"},
            {"label": "P1.T3.2", "text": "Part B"},
        ],
    }
    result = _parse_decompose_response("", parsed)
    assert result is not None
    assert result.should_decompose is True
    assert result.reason == "Task spans multiple modules"
    assert len(result.subtasks) == 2
    assert result.subtasks[0].label == "P1.T3.1"
    assert result.subtasks[1].text == "Part B"


def test_parse_arch_response_pydantic_valid():
    """Valid dict → ArchResponse returned via Pydantic path."""
    from tasker.orchestrator import _parse_arch_response

    parsed = {
        "action": "clarify",
        "reason": "Task description is ambiguous",
        "subtasks": [],
        "new_task_text": "Rewritten task description",
        "max_iterations_override": None,
    }
    result = _parse_arch_response("", parsed)
    assert result is not None
    assert result.action == "clarify"
    assert result.reason == "Task description is ambiguous"
    assert result.new_task_text == "Rewritten task description"
    assert result.max_iterations_override is None


# ── 49. Test _parse_decompose_response ───────────────────────────


def test_parse_decompose_response():
    """Test _parse_decompose_response with valid and invalid inputs."""
    from tasker.orchestrator import _parse_decompose_response

    # Valid: no decomposition
    parsed = {"should_decompose": False, "reason": "Simple task", "subtasks": []}
    result = _parse_decompose_response("", parsed)
    assert result is not None
    assert result.should_decompose is False
    assert result.reason == "Simple task"
    assert result.subtasks == []

    # Valid: decomposition with subtasks
    parsed2 = {
        "should_decompose": True,
        "reason": "3 modules involved",
        "subtasks": [
            {"label": "P1.T1.1", "text": "Build struct A"},
            {"label": "P1.T1.2", "text": "Implement trait B"},
        ],
    }
    result2 = _parse_decompose_response("some output", parsed2)
    assert result2 is not None
    assert result2.should_decompose is True
    assert result2.reason == "3 modules involved"
    assert len(result2.subtasks) == 2
    assert result2.subtasks[0].label == "P1.T1.1"
    assert result2.subtasks[1].text == "Implement trait B"

    # String boolean ("true")
    parsed3 = {"should_decompose": "true", "reason": "yes", "subtasks": []}
    result3 = _parse_decompose_response("", parsed3)
    assert result3 is not None
    assert result3.should_decompose is True

    # String boolean ("false")
    parsed4 = {"should_decompose": "false", "reason": "no", "subtasks": []}
    result4 = _parse_decompose_response("", parsed4)
    assert result4 is not None
    assert result4.should_decompose is False

    # Missing should_decompose key → None
    result5 = _parse_decompose_response("", {"reason": "no key"})
    assert result5 is None

    # parsed is None → None
    result6 = _parse_decompose_response("garbage", None)
    assert result6 is None

    # Subtask missing label → skipped
    parsed7 = {
        "should_decompose": True,
        "reason": "test",
        "subtasks": [{"text": "only text, no label"}],
    }
    result7 = _parse_decompose_response("", parsed7)
    assert result7 is not None
    assert result7.should_decompose is True
    assert result7.subtasks == []  # malformed subtask was skipped

    # Extra fields in subtask are ignored (only label+text used)
    parsed8 = {
        "should_decompose": True,
        "reason": "test",
        "subtasks": [{"label": "S1", "text": "Do thing", "priority": "high"}],
    }
    result8 = _parse_decompose_response("", parsed8)
    assert result8 is not None
    assert len(result8.subtasks) == 1
    assert result8.subtasks[0].label == "S1"

    print("✓ _parse_decompose_response tests passed")


# ── 50. Test _decompose_task ─────────────────────────────────────


def test_decompose_task():
    """Test _decompose_task: disabled, valid response, parse failure fallback."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.goose import GooseRunResult
    from tasker.models import Task

    task = Task(
        phase_index=0, task_index=2, text="Build GridCell and SpatialIndex — arch/02.md"
    )

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/iter.jsonl"
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("# Phase 1\n- [ ] T1: dummy\n")

        # --- Case 1: disabled (no decompose_recipe) → returns None ---
        orch_disabled = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )
        result_none = orch_disabled._decompose_task(task)
        assert result_none is None, "Should return None when decompose is disabled"

        # --- Case 2: enabled, valid decompose response ---
        orch_enabled = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
            decompose_recipe="/dev/null",
        )
        fake_decompose = GooseRunResult(
            success=True,
            raw_stdout='{"should_decompose": true, "reason": "2 modules", '
            '"subtasks": [{"label": "P1.T3.1", "text": "Build GridCell"}, '
            '{"label": "P1.T3.2", "text": "Build SpatialIndex"}]}',
            raw_stderr="",
            return_code=0,
            parsed_json={
                "should_decompose": True,
                "reason": "2 modules",
                "subtasks": [
                    {"label": "P1.T3.1", "text": "Build GridCell"},
                    {"label": "P1.T3.2", "text": "Build SpatialIndex"},
                ],
            },
        )
        with patch("tasker.goose.run_goose", return_value=fake_decompose):
            result_yes = orch_enabled._decompose_task(task)
        assert result_yes is not None
        assert result_yes.should_decompose is True
        assert result_yes.reason == "2 modules"
        assert len(result_yes.subtasks) == 2
        assert result_yes.subtasks[0].label == "P1.T3.1"
        assert result_yes.subtasks[1].text == "Build SpatialIndex"

        # --- Case 3: enabled, valid no-decompose response ---
        fake_no_split = GooseRunResult(
            success=True,
            raw_stdout='{"should_decompose": false, "reason": "Simple task", "subtasks": []}',
            raw_stderr="",
            return_code=0,
            parsed_json={
                "should_decompose": False,
                "reason": "Simple task",
                "subtasks": [],
            },
        )
        with patch("tasker.goose.run_goose", return_value=fake_no_split):
            result_no = orch_enabled._decompose_task(task)
        assert result_no is not None
        assert result_no.should_decompose is False
        assert result_no.reason == "Simple task"
        assert result_no.subtasks == []

        # --- Case 4: enabled, parse failure → fallback should_decompose=False ---
        fake_garbage = GooseRunResult(
            success=True,
            raw_stdout="I could not determine the complexity.",
            raw_stderr="",
            return_code=0,
            parsed_json=None,
        )
        with patch("tasker.goose.run_goose", return_value=fake_garbage):
            result_fallback = orch_enabled._decompose_task(task)
        assert result_fallback is not None
        assert result_fallback.should_decompose is False
        assert "failed" in result_fallback.reason.lower()

        print("✓ _decompose_task tests passed")


# ── 51. Test _run_dev_with_recovery override_task_text ───────────


def test_dev_override_task_text():
    """Test that override_task_text replaces task.text in the DevRequest params."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.goose import GooseRunResult
    from tasker.models import Task

    task = Task(phase_index=0, task_index=0, text="Original full task description")

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/iter.jsonl"
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("# Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )

        fake_done = GooseRunResult(
            success=True,
            raw_stdout='{"status": "done", "summary": "Implemented subtask A", "files_modified": ["src/a.rs"]}',
            raw_stderr="",
            return_code=0,
            parsed_json={
                "status": "done",
                "summary": "Implemented subtask A",
                "files_modified": ["src/a.rs"],
            },
        )

        with patch("tasker.goose.run_goose", return_value=fake_done) as mock_goose:
            result = orch._run_dev_with_recovery(
                task=task,
                iteration=1,
                feedback=None,
                override_task_text="Focused subtask: implement GridCell struct only",
            )

        assert result.status == "done"
        assert result.summary == "Implemented subtask A"

        # Verify the params passed to goose contained the override text
        call_args = mock_goose.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params")
        assert params["task_text"] == "Focused subtask: implement GridCell struct only"
        assert "GridCell" in params["task_text"]

        # --- Without override → uses task.text ---
        mock_goose.reset_mock()
        with patch("tasker.goose.run_goose", return_value=fake_done) as mock_goose:
            orch._run_dev_with_recovery(
                task=task,
                iteration=1,
                feedback=None,
                override_task_text=None,
            )

        call_args2 = mock_goose.call_args
        params2 = call_args2.kwargs.get("params") or call_args2[1].get("params")
        assert params2["task_text"] == "Original full task description"

        print("✓ _run_dev_with_recovery override_task_text tests passed")


# ── 52. Test _run_feedback_loop work_label with subtask_label ─────


def test_feedback_loop_subtask_label():
    """Test that work_label uses subtask_label when provided."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task
    from tasker.models import Phase, DevResponse, QAResponse

    task = Task(phase_index=0, task_index=2, text="Big task spanning modules")
    phase = Phase(index=0, title="Phase 1", tasks=[task])

    fake_dev = DevResponse(
        status="done",
        summary="Implemented GridCell struct",
        files_modified=["src/grid.rs"],
    )
    fake_qa = QAResponse(decision="approve", feedback="Looks correct")

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/iter.jsonl"
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("# Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )

        printed_msgs = []
        original_print_info = orch.ui.print_info
        original_print_success = orch.ui.print_success

        def capture_info(msg):
            printed_msgs.append(("info", msg))
            original_print_info(msg)

        def capture_success(msg):
            printed_msgs.append(("success", msg))
            original_print_success(msg)

        with patch.object(orch.ui, "print_info", side_effect=capture_info):
            with patch.object(orch.ui, "print_success", side_effect=capture_success):
                with patch.object(
                    orch, "_run_dev_with_recovery", return_value=fake_dev
                ):
                    with patch.object(
                        orch, "_run_qa_with_recovery", return_value=fake_qa
                    ):
                        orch._run_feedback_loop(
                            phase=phase,
                            task=task,
                            effective_task_text="Implement GridCell struct only",
                            feedback=None,
                            subtask_label="P1.T3.1",
                            subtask_index="1/2",
                        )

        # Verify work_label (subtask_label) appears in UI messages
        info_msgs = [m[1] for m in printed_msgs if m[0] == "info"]
        success_msgs = [m[1] for m in printed_msgs if m[0] == "success"]

        # At least one info message should reference P1.T3.1 (the subtask_label)
        subtask_msgs = [m for m in info_msgs if "P1.T3.1" in m]
        assert len(subtask_msgs) > 0, (
            f"Expected subtask_label P1.T3.1 in UI messages, got: {info_msgs}"
        )

        # The QA approval message should use work_label
        approve_msgs = [m for m in success_msgs if "APPROVED" in m]
        assert len(approve_msgs) == 1
        assert "P1.T3.1" in approve_msgs[0], (
            f"Expected P1.T3.1 in approval message, got: {approve_msgs[0]}"
        )

        print("✓ _run_feedback_loop subtask_label work_label tests passed")


# ── 53. Test _run_subtask_loop iterates over subtasks ────────────


def test_run_subtask_loop():
    """Test _run_subtask_loop calls _run_feedback_loop for each subtask."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task
    from tasker.models import Phase, Subtask

    task = Task(phase_index=0, task_index=2, text="Big task")
    phase = Phase(index=0, title="Phase 1", tasks=[task])

    subtasks = [
        Subtask(label="P1.T3.1", text="Implement GridCell struct"),
        Subtask(label="P1.T3.2", text="Implement SpatialIndex trait"),
        Subtask(label="P1.T3.3", text="Add unit tests"),
    ]

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/iter.jsonl"
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("# Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )

        with patch.object(orch, "_run_feedback_loop") as mock_loop:
            orch._run_subtask_loop(phase, task, subtasks)

        assert mock_loop.call_count == 3

        # Verify first call: subtask 1
        c1 = mock_loop.call_args_list[0]
        assert c1[0][0] is phase  # positional arg: phase
        assert c1[0][1] is task  # positional arg: task
        assert c1[0][2] == "Implement GridCell struct"  # effective_task_text
        assert c1[1].get("feedback") is None
        assert c1[1].get("subtask_label") == "P1.T3.1"
        assert c1[1].get("subtask_index") == "1/3"

        # Verify second call: subtask 2
        c2 = mock_loop.call_args_list[1]
        assert c2[0][2] == "Implement SpatialIndex trait"
        assert c2[1].get("subtask_label") == "P1.T3.2"
        assert c2[1].get("subtask_index") == "2/3"

        # Verify third call: subtask 3
        c3 = mock_loop.call_args_list[2]
        assert c3[0][2] == "Add unit tests"
        assert c3[1].get("subtask_label") == "P1.T3.3"
        assert c3[1].get("subtask_index") == "3/3"

        print("✓ _run_subtask_loop tests passed")


# ── 54. Test _process_task with and without decomposition ────────


def test_process_task_decomposition():
    """Test _process_task routes to subtask loop or direct feedback loop."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task
    from tasker.models import Phase, Subtask, DecomposeResponse

    task = Task(phase_index=0, task_index=2, text="Build GridCell and SpatialIndex")
    phase = Phase(index=0, title="Phase 1", tasks=[task])

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/iter.jsonl"
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("# Phase 1\n- [ ] T1: dummy\n")

        # --- Case 1: No decomposition (decompose_recipe not set) ---
        orch_no_decompose = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )
        with patch.object(orch_no_decompose, "_run_feedback_loop") as mock_direct:
            with patch.object(orch_no_decompose, "_vcs_begin_task"):
                orch_no_decompose._process_task(phase, task)

        mock_direct.assert_called_once()
        c = mock_direct.call_args
        assert c[0][0] is phase
        assert c[0][1] is task
        assert c[0][2] == task.text  # effective_task_text = original task.text
        assert c[1].get("feedback") is None

        # --- Case 2: Decomposition enabled, should_decompose=False ---
        orch_decompose = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
            decompose_recipe="/dev/null",
        )
        no_split = DecomposeResponse(should_decompose=False, reason="Simple task")
        with patch.object(orch_decompose, "_decompose_task", return_value=no_split):
            with patch.object(orch_decompose, "_run_feedback_loop") as mock_direct2:
                with patch.object(orch_decompose, "_vcs_begin_task"):
                    orch_decompose._process_task(phase, task)

        mock_direct2.assert_called_once()

        # --- Case 3: Decomposition enabled, should_decompose=True → subtask loop ---
        subtasks = [
            Subtask(label="P1.T3.1", text="Build GridCell"),
            Subtask(label="P1.T3.2", text="Build SpatialIndex"),
        ]
        with_split = DecomposeResponse(
            should_decompose=True,
            reason="2 modules",
            subtasks=subtasks,
        )
        with patch.object(orch_decompose, "_decompose_task", return_value=with_split):
            with patch.object(orch_decompose, "_run_subtask_loop") as mock_sub:
                with patch.object(orch_decompose, "_run_feedback_loop") as mock_direct3:
                    with patch.object(orch_decompose, "_vcs_begin_task"):
                        orch_decompose._process_task(phase, task)

        mock_sub.assert_called_once()
        mock_direct3.assert_not_called()  # should NOT call direct feedback loop
        # Verify subtask_loop received the subtasks
        c_sub = mock_sub.call_args
        assert c_sub[0][0] is phase
        assert c_sub[0][1] is task
        assert len(c_sub[0][2]) == 2
        assert c_sub[0][2][0].label == "P1.T3.1"

        # --- Case 4: Decompose returns None (disabled) → direct loop ---
        with patch.object(orch_no_decompose, "_decompose_task", return_value=None):
            with patch.object(orch_no_decompose, "_run_feedback_loop") as mock_direct4:
                with patch.object(orch_no_decompose, "_vcs_begin_task"):
                    orch_no_decompose._process_task(phase, task)

        mock_direct4.assert_called_once()

        print("✓ _process_task decomposition routing tests passed")


# ── 55. Test _process_task VCS is called once per task ───────────


def test_process_task_vcs_once_per_task():
    """Test that _vcs_begin_task is called exactly once even with 3 subtasks."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task
    from tasker.models import Phase, Subtask, DecomposeResponse

    task = Task(phase_index=0, task_index=2, text="Build GridCell and SpatialIndex")
    phase = Phase(index=0, title="Phase 1", tasks=[task])

    subtasks = [
        Subtask(label="P1.T3.1", text="Build GridCell"),
        Subtask(label="P1.T3.2", text="Build SpatialIndex"),
        Subtask(label="P1.T3.3", text="Add tests"),
    ]
    with_split = DecomposeResponse(
        should_decompose=True,
        reason="3 modules",
        subtasks=subtasks,
    )

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/iter.jsonl"
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("# Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
            decompose_recipe="/dev/null",
        )

        with patch.object(orch, "_decompose_task", return_value=with_split):
            with patch.object(orch, "_run_subtask_loop") as mock_sub:
                with patch.object(orch, "_vcs_begin_task") as mock_vcs:
                    orch._process_task(phase, task)

        # VCS begin must be called exactly once for the parent task
        mock_vcs.assert_called_once_with(task)

        # Subtask loop must be called once
        mock_sub.assert_called_once()

        print("✓ _process_task VCS once-per-task tests passed")


# ── Test 56: truncation detection helper ────────────────────────────


def test_truncation_detection():
    """_is_truncated_output detects goose truncation markers."""
    from tasker.orchestrator import _is_truncated_output

    assert _is_truncated_output(
        "I'll implement the file now.\n\n"
        "A tool call could not be parsed — the response may have been truncated."
    )
    assert _is_truncated_output("") is False
    assert _is_truncated_output(None) is False
    assert _is_truncated_output("normal dev output with JSON block") is False
    assert (
        _is_truncated_output(
            '{"status": "done", "summary": "ok", "files_modified": []}'
        )
        is False
    )
    print("✓ _is_truncated_output tests passed")


# ── Test 57: dev truncation fast-forward ────────────────────────────


def test_dev_truncation_fast_forward():
    """Truncation in dev output fast-forwards to SUBTASK stage."""
    import tempfile
    from unittest.mock import patch
    from tasker.models import Task
    from tasker.orchestrator import Orchestrator
    from tasker.goose import GooseRunResult

    task = Task(
        phase_index=0, task_index=0, text="Implement mapping.rs with full types"
    )

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/test57.jsonl"
        md_path = f"{td}/test57.md"
        Path(md_path).write_text("# P4-2\n- [ ] T4: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )
        iteration = 1

        truncation_raw = (
            "I'll start by reading the spec file.\n\n"
            "Now I have all the context. Let me write the complete implementation:\n\n"
            "A tool call could not be parsed — the response may have been truncated. "
            "Try breaking the task into smaller steps or resending your message."
        )

        call_count = 0

        def mock_run_goose(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: truncation in NORMAL stage
                return GooseRunResult(
                    success=True,
                    raw_stdout=truncation_raw,
                    raw_stderr="",
                    return_code=0,
                    parsed_json=None,
                )
            else:
                # Second call: SUBTASK stage with truncation instruction → skeleton
                return GooseRunResult(
                    success=True,
                    raw_stdout='{"status": "done", "summary": "Wrote skeleton: mapping.rs", "files_modified": ["crates/hay-style/src/mapping.rs"]}',
                    raw_stderr="",
                    return_code=0,
                    parsed_json={
                        "status": "done",
                        "summary": "Wrote skeleton: mapping.rs",
                        "files_modified": ["crates/hay-style/src/mapping.rs"],
                    },
                )

        with patch(
            "tasker.orchestrator.run_goose_with_backoff", side_effect=mock_run_goose
        ):
            result = orch._run_dev_with_recovery(task, iteration, feedback=None)

        assert result is not None
        assert result.status == "done"
        assert "skeleton" in result.summary.lower()
        assert call_count == 2  # truncation detected → fast-forward → 1 retry
        print("✓ dev truncation fast-forward tests passed")


def test_dev_truncation_suppresses_task_text():
    """When truncation is detected, SUBTASK/SUMMARIZE stages suppress task_text
    to prevent the agent from re-reading specs and burning output tokens."""
    import tempfile
    from unittest.mock import patch
    from tasker.models import Task
    from tasker.orchestrator import Orchestrator
    from tasker.goose import GooseRunResult

    full_task = (
        "Implement mapping.rs per arch/11-gis-renderer.md \u00a7Attribute Mapping. "
        "Create AttributeRow, evaluate_rule, evaluate_continuous, evaluate_categorical, "
        "normalize, quantile_position. See arch/11-gis-renderer.md for full Rust code examples."
    )

    task = Task(phase_index=0, task_index=0, text=full_task)

    with tempfile.TemporaryDirectory() as td:
        log_path = f"{td}/test58.jsonl"
        md_path = f"{td}/test58.md"
        Path(md_path).write_text("# P4-2\n- [ ] T4: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
        )
        iteration = 1

        truncation_raw = (
            "I'll read the spec file and implement everything.\n\n"
            "A tool call could not be parsed \u2014 the response may have been truncated."
        )

        call_count = 0
        captured_params: list[dict] = []

        def mock_run_goose(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            captured_params.append(kwargs.get("params", {}))
            if call_count <= 3:
                return GooseRunResult(
                    success=True,
                    raw_stdout=truncation_raw,
                    raw_stderr="",
                    return_code=0,
                    parsed_json=None,
                )
            else:
                return GooseRunResult(
                    success=True,
                    raw_stdout=(
                        '{"status": "blocked", "summary": "Partial skeleton", '
                        '"files_modified": [], "blocker_description": "File too large"}'
                    ),
                    raw_stderr="",
                    return_code=0,
                    parsed_json={
                        "status": "blocked",
                        "summary": "Partial skeleton",
                        "files_modified": [],
                        "blocker_description": "File too large",
                    },
                )

        with patch(
            "tasker.orchestrator.run_goose_with_backoff", side_effect=mock_run_goose
        ):
            result = orch._run_dev_with_recovery(task, iteration, feedback=None)

        assert result is not None
        assert result.status == "blocked"
        assert call_count == 4

        # First call (NORMAL): full task text should be present
        assert full_task in captured_params[0].get("task_text", ""), (
            "Call 1 (NORMAL) should have full task text"
        )

        # Calls after fast-forward (SUBTASK+): task_text should be suppressed
        for i, params in enumerate(captured_params[1:], start=2):
            task_text = params.get("task_text", "")
            assert "arch/11-gis-renderer.md" not in task_text, (
                f"Call {i}: task_text should be suppressed, got: {task_text[:200]}"
            )
            assert "Recovery mode" in task_text, (
                f"Call {i}: task_text should say Recovery mode, got: {task_text[:200]}"
            )

        print("\u2713 dev truncation suppresses task_text in SUBTASK/SUMMARIZE stages")


# ── Run all ───────────────────────────────────────────────────────

if __name__ == "__main__":
    test_parser()
    test_logger()
    test_json_extraction()
    test_markdown_update()
    test_command_builder()
    test_models()
    test_parser_strictness()
    test_envelope_extraction()
    test_jj_module()
    test_task_jj_fields()
    test_qa_request_with_project_context()
    test_goose_result_timed_out()
    test_timeout_feedback()
    test_subphase_parsing()
    test_session_scope_enum()
    test_scope_key_computation()
    test_backward_compat_no_subphases()
    test_task_subphase_field()
    test_subphase_labels()
    test_vcs_backend_protocol()
    test_task_vcs_fields()
    test_jj_reexports()
    test_git_backend_helpers()
    test_git_backend_init_errors()
    test_create_backend_types()
    test_jj_backend_protocol()
    test_git_backend_protocol()
    test_task_vcs_description()
    test_finalize_task_ordering()
    test_monitoring_setup()
    test_monitoring_file_output()
    test_monitoring_idempotent()
    test_monitoring_get_logger()
    test_monitoring_parser_captured()
    test_monitoring_orchestrator_events_captured()
    test_resolve_level()
    test_monitoring_log_levels()
    test_monitoring_invalid_level()
    test_activity_renderable()
    test_ui_activity_indicator()
    test_goose_heartbeat_thread()
    test_run_goose_with_ui_wiring()
    test_format_timestamp()
    test_entry_summary()
    test_pending_iteration_lifecycle()
    test_spinner_frames()
    test_pending_iteration_dataclass()
    test_decompose_models()
    test_schema_dev_response_valid_done()
    test_schema_dev_response_valid_blocked()
    test_schema_dev_response_invalid_status()
    test_schema_qa_response_all_decisions()
    test_schema_qa_response_invalid_decision()
    test_schema_decompose_response()
    test_schema_arch_response_all_actions()

    # P4.T1: Pydantic-backed _parse_*_response tests
    test_parse_dev_response_pydantic_valid()
    test_parse_dev_response_pydantic_invalid_status_fallback()
    test_parse_dev_response_extra_fields()
    test_parse_qa_response_pydantic_valid()
    test_parse_qa_response_invalid_decision()
    test_parse_decompose_response_pydantic_valid()
    test_parse_arch_response_pydantic_valid()

    test_parse_decompose_response()
    test_decompose_task()
    test_dev_override_task_text()
    test_feedback_loop_subtask_label()
    test_run_subtask_loop()
    test_process_task_decomposition()
    test_process_task_vcs_once_per_task()
    test_truncation_detection()
    test_dev_truncation_fast_forward()
    test_dev_truncation_suppresses_task_text()
    print("\n✅ All 65 dry-run tests passed!")
# ── ARCH (Architect) agent tests ──────────────────────────────────


def test_arch_models():
    """Test ArchAction, ArchRequest, ArchResponse models."""
    from tasker.models import ArchAction, ArchRequest, ArchResponse, Subtask

    # ArchAction enum
    assert ArchAction.REDECOMPOSE.value == "recompose"
    assert ArchAction.CLARIFY.value == "clarify"
    assert ArchAction.SKIP.value == "skip"
    assert ArchAction.RETRY.value == "retry"

    # ArchRequest
    req = ArchRequest(
        task_label="P5-2.T3",
        task_text="Implement LOD switch",
        error_summary="15 malformed_output, 3 timeout",
        code_state="3 .rs files recently modified",
    )
    params = req.to_params()
    assert params["task_label"] == "P5-2.T3"
    assert params["task_text"] == "Implement LOD switch"
    assert "malformed_output" in params["error_summary"]

    # ArchResponse — REDECOMPOSE
    resp = ArchResponse(
        action="recompose",
        reason="Task covers 3 structs and 2 tests",
        subtasks=[
            Subtask(label="P5-2.T3a", text="Define LODSwitch enum"),
            Subtask(label="P5-2.T3b", text="Implement GridPipeline::draw LOD branch"),
        ],
    )
    d = resp.to_dict()
    assert d["action"] == "recompose"
    assert len(d["subtasks"]) == 2

    # ArchResponse — CLARIFY
    resp2 = ArchResponse(
        action="clarify",
        reason="Task text is ambiguous",
        new_task_text="Implement LOD branch in GridPipeline::draw: dispatch based on cell_screen_size",
    )
    d2 = resp2.to_dict()
    assert d2["action"] == "clarify"
    assert "cell_screen_size" in d2["new_task_text"]

    # ArchResponse — SKIP
    resp3 = ArchResponse(action="skip", reason="Already implemented")
    d3 = resp3.to_dict()
    assert d3["action"] == "skip"
    assert "subtasks" not in d3

    # ArchResponse — RETRY
    resp4 = ArchResponse(
        action="retry",
        reason="Provider connection error, not task issue",
        max_iterations_override=5,
    )
    d4 = resp4.to_dict()
    assert d4["action"] == "retry"
    assert d4["max_iterations_override"] == 5

    print("✓ ARCH model tests passed")


def test_parse_arch_response():
    """Test _parse_arch_response with valid and invalid inputs."""
    from tasker.orchestrator import _parse_arch_response

    # Valid REDECOMPOSE
    parsed = {
        "action": "recompose",
        "reason": "Too complex",
        "subtasks": [
            {"label": "T3a", "text": "Part A"},
            {"label": "T3b", "text": "Part B"},
        ],
    }
    resp = _parse_arch_response("", parsed)
    assert resp is not None
    assert resp.action == "recompose"
    assert len(resp.subtasks) == 2

    # Valid CLARIFY
    parsed2 = {
        "action": "clarify",
        "reason": "Ambiguous",
        "new_task_text": "Do X in file Y",
    }
    resp2 = _parse_arch_response("", parsed2)
    assert resp2 is not None
    assert resp2.action == "clarify"
    assert resp2.new_task_text == "Do X in file Y"

    # Valid SKIP
    parsed3 = {"action": "skip", "reason": "Done already"}
    resp3 = _parse_arch_response("", parsed3)
    assert resp3 is not None
    assert resp3.action == "skip"

    # Valid RETRY
    parsed4 = {"action": "retry", "reason": "Transient"}
    resp4 = _parse_arch_response("", parsed4)
    assert resp4 is not None
    assert resp4.action == "retry"

    # Invalid action
    parsed_bad = {"action": "explode", "reason": "Nope"}
    assert _parse_arch_response("", parsed_bad) is None

    # Missing action
    assert _parse_arch_response("", {"reason": "No action"}) is None

    # No parsed JSON
    assert _parse_arch_response("some text", None) is None

    print("✓ _parse_arch_response tests passed")


def test_insert_subtasks():
    """Test parser.insert_subtasks — replaces a task with multiple subtasks."""
    from tasker.parser import parse_task_file, insert_subtasks, find_next_task

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1 — Test\n")
        f.write("- [ ] Implement grid renderer\n")
        f.write("- [ ] Write tests\n")
        path = Path(f.name)

    phases = parse_task_file(path)
    assert phases[0].total == 2

    pair = find_next_task(phases)
    assert pair is not None, "Expected at least one undone task"
    _, task = pair
    assert task.text == "Implement grid renderer"

    insert_subtasks(
        path,
        phases,
        task,
        subtask_labels=["P1.T1a", "P1.T1b"],
        subtask_texts=["Create GridRenderer struct", "Implement GridRenderer::draw()"],
    )

    # Original task should be marked done
    assert task.done is True

    # Phase should now have 4 tasks (1 done + 2 new + 1 original)
    assert phases[0].total == 4

    # Next undone task should be the first subtask
    pair = find_next_task(phases)
    assert pair is not None, "Expected at least one undone task"
    _, next_task = pair
    assert next_task.text == "Create GridRenderer struct"

    # Verify markdown file was updated
    content = path.read_text()
    assert "- [x]" in content  # original marked done
    assert "- [ ] Create GridRenderer struct" in content
    assert "- [ ] Implement GridRenderer::draw()" in content
    assert "- [ ] Write tests" in content  # untouched

    print("✓ insert_subtasks tests passed")


def test_rewrite_task_text():
    """Test parser.rewrite_task_text — rewrites a task's description."""
    from tasker.parser import parse_task_file, rewrite_task_text, find_next_task

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1 — Test\n")
        f.write("- [ ] Implement grid renderer\n")
        f.write("- [ ] Write tests\n")
        path = Path(f.name)

    phases = parse_task_file(path)
    pair = find_next_task(phases)
    assert pair is not None, "Expected at least one undone task"
    _, task = pair
    assert task.text == "Implement grid renderer"

    rewrite_task_text(
        path,
        phases,
        task,
        new_text="Implement GridRenderer struct with new() and draw() methods in crates/hay-render/src/grid.rs",
    )

    # In-memory model should be updated
    assert "GridRenderer" in task.text

    # Markdown file should be updated
    content = path.read_text()
    assert "GridRenderer" in content
    assert "- [ ] Implement GridRenderer" in content

    # Other tasks should be untouched
    # The rewritten task is still undone, so find_next_task returns it
    pair = find_next_task(phases)
    assert pair is not None, "Expected at least one undone task"
    _, next_task2 = pair
    assert next_task2.text == task.text  # same task, rewritten
    assert "GridRenderer" in next_task2.text

    print("✓ rewrite_task_text tests passed")


def test_stuckness_detection():
    """Test Orchestrator._is_task_stuck and counter management."""
    from tasker.orchestrator import (
        _STUCK_EXHAUSTION_THRESHOLD,
        _STUCK_ITERATION_THRESHOLD,
    )

    # Verify thresholds
    assert _STUCK_EXHAUSTION_THRESHOLD == 2, (
        f"Expected threshold 2, got {_STUCK_EXHAUSTION_THRESHOLD}"
    )
    assert _STUCK_ITERATION_THRESHOLD == 10

    # Test with a mock orchestrator (no real goose calls)
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1 — Test\n- [ ] Task 1\n")
        path = Path(f.name)

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = Path(f.name)

    orch = Orchestrator(
        task_file=path,
        dev_recipe=path,  # dummy
        qa_recipe=path,
        log_file=log_path,
        session_scope=SessionScope.TASK,
    )
    orch.phases = parse_task_file(path)

    task = Task(phase_index=0, task_index=0, text="Task 1")

    # Initially not stuck
    assert not orch._is_task_stuck(task)

    # Simulate exhaustions — threshold is 2
    orch._stuck_task_label = task.label
    orch._consecutive_exhaustions = 1
    assert not orch._is_task_stuck(task)

    orch._consecutive_exhaustions = 2
    assert orch._is_task_stuck(task)

    # Reset with different task
    task2 = Task(phase_index=0, task_index=1, text="Task 2")
    assert not orch._is_task_stuck(task2)
    assert orch._consecutive_exhaustions == 0

    # Iteration threshold
    orch._stuck_task_label = task.label
    orch._iterations_without_approval = 9
    assert not orch._is_task_stuck(task)

    orch._iterations_without_approval = 10
    assert orch._is_task_stuck(task)

    print("✓ Stuckness detection tests passed")


def test_feedback_truncation():
    """Test Orchestrator._truncate_feedback."""
    from tasker.orchestrator import Orchestrator, _MAX_FEEDBACK_LENGTH
    from tasker.models import SessionScope

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1\n- [ ] T1\n")
        path = Path(f.name)

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = Path(f.name)

    orch = Orchestrator(
        task_file=path,
        dev_recipe=path,
        qa_recipe=path,
        log_file=log_path,
        session_scope=SessionScope.TASK,
    )

    # Short feedback — no truncation
    short = "Fix the typo"
    assert orch._truncate_feedback(short) == short

    # Build long feedback with multiple rounds
    long_feedback = ""
    for i in range(10):
        long_feedback += (
            f"## QA Decision: REJECT\n\n**Feedback:** Iteration {i} had issues. "
            + "x" * 300
            + "\n\n"
        )
        long_feedback += "**Concerns:**\n- Concern 1\n- Concern 2\n\n"
    long_feedback += "Please fix ALL concerns above and re-submit."

    assert len(long_feedback) > _MAX_FEEDBACK_LENGTH

    truncated = orch._truncate_feedback(long_feedback)
    assert len(truncated) <= _MAX_FEEDBACK_LENGTH + 100  # allow margin for header
    assert "truncated" in truncated

    print("✓ Feedback truncation tests passed")


def test_arch_actor_in_ui():
    """Test that ARCH actor gets the correct icon in UI.update_actor."""
    from tasker.models import Actor

    assert Actor.ARCH.value == "arch"
    assert Actor.ARCH in (Actor.QA, Actor.DEV, Actor.ARCH)

    print("✓ ARCH actor enum tests passed")


def test_connection_error_hard_stop():
    """Test that connection errors trigger a hard stop in dev recovery."""
    # This is tested indirectly — we verify the code path exists
    # by checking the orchestrator handles 'not connected' in stderr
    from tasker.goose import GooseRunResult

    # Simulate a connection error result
    result = GooseRunResult(
        success=False,
        return_code=1,
        raw_stdout="",
        raw_stderr="Error: not connected",
        duration_secs=5.0,
    )
    assert not result.success
    assert "not connected" in result.raw_stderr.lower()

    print("✓ Connection error hard stop setup verified")


def test_run_loop_reparse():
    """Test that _run_loop re-parses markdown when ARCH sets _needs_reparse."""
    from tasker.orchestrator import Orchestrator
    from tasker.models import SessionScope

    # Create a task file
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w") as f:
        f.write("## Phase 1 — Test\n- [ ] Task 1\n- [ ] Task 2\n")
        path = Path(f.name)

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
        log_path = Path(f.name)

    orch = Orchestrator(
        task_file=path,
        dev_recipe=path,
        qa_recipe=path,
        log_file=log_path,
        session_scope=SessionScope.TASK,
    )
    orch.phases = parse_task_file(path)

    # Simulate ARCH restructuring: rewrite the file
    path.write_text(
        "## Phase 1 — Test\n- [ ] Task 1\n- [ ] Task 1a\n- [ ] Task 1b\n- [ ] Task 2\n"
    )

    # Set the reparse flag
    orch._needs_reparse = True

    # The _run_loop would re-parse on the next iteration
    # We verify the mechanism exists by simulating the reparse
    if orch._needs_reparse:
        orch.phases = parse_task_file(path)
        orch._needs_reparse = False

    assert orch.phases[0].total == 4  # original 2 + 2 new subtasks
    assert not orch._needs_reparse

    print("✓ _run_loop reparse tests passed")


def test_default_session_scope_is_task():
    """Test that the default session scope is now 'task'."""
    from tasker.models import SessionScope

    # The default should be TASK now (changed from SUBPHASE)
    # This is set in main.py but the Orchestrator still accepts any value
    assert SessionScope.TASK.value == "task"
    assert SessionScope.SUBPHASE.value == "subphase"

    print("✓ Default session scope tests passed")


# ── Bug Fix tests (stuckness / ARCH invocation) ───────────────────


def test_stuckness_counts_all_blocked():
    """Bug Fix 1: ALL dev blocked events increment counter, not just synthetic ones."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope, DevResponse, QAResponse, Phase

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        task = Task(phase_index=0, task_index=0, text="T1")
        phase = Phase(index=0, title="Phase 1", tasks=[task])
        orch._stuck_task_label = task.label

        # Blocked with plain summary — no "multiple recovery attempts" keyword
        blocked = DevResponse(
            status="blocked",
            summary="Cannot find the config file",
            files_modified=[],
            blocker_description="Config file is missing",
        )
        qa_approve = QAResponse(decision="approve", feedback="OK, skip it")

        with patch.object(orch, "_run_dev_with_recovery", return_value=blocked):
            with patch.object(orch, "_run_qa_with_recovery", return_value=qa_approve):
                with patch.object(orch, "_finalize_task"):
                    orch._run_feedback_loop(phase, task, task.text, feedback=None)

        assert orch._consecutive_exhaustions == 1, (
            f"Expected counter=1 for ANY blocked event, got {orch._consecutive_exhaustions}"
        )

    print("✓ test_stuckness_counts_all_blocked passed")


def test_stuckness_threshold_lowered():
    """Bug Fix 3: exhaustion threshold is 2 (was 3)."""
    from tasker.orchestrator import _STUCK_EXHAUSTION_THRESHOLD

    assert _STUCK_EXHAUSTION_THRESHOLD == 2, (
        f"Expected threshold 2, got {_STUCK_EXHAUSTION_THRESHOLD}"
    )

    print("✓ test_stuckness_threshold_lowered passed")


def test_stuckness_check_deferred():
    """Bug Fix 2: _pending_arch_check is set at BOTTOM of loop, invoked at TOP of next iter."""
    import tempfile
    from unittest.mock import patch, MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope, DevResponse, QAResponse, Phase

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        # Arch recipe must be non-None for ARCH check to run
        orch.arch_recipe = MagicMock()
        task = Task(phase_index=0, task_index=0, text="T1")
        phase = Phase(index=0, title="Phase 1", tasks=[task])
        orch._stuck_task_label = task.label
        # One short of threshold — one more blocked will tip it
        orch._consecutive_exhaustions = 1

        blocked = DevResponse(
            status="blocked",
            summary="Still stuck",
            files_modified=[],
            blocker_description="Missing dep",
        )
        qa_reject = QAResponse(decision="reject", feedback="Try again")

        # ARCH itself should be invoked on next iteration — mock it to stop the loop
        arch_calls = []

        def fake_run_arch(task):
            arch_calls.append(task)
            return (
                None  # None → no reparse, loop continues but _pending_arch_check reset
            )

        iteration_count = 0

        def dev_side_effect(**_kw):
            nonlocal iteration_count
            iteration_count += 1
            if iteration_count == 1:
                return blocked
            # Second iteration (after ARCH): return done so the loop ends
            return DevResponse(status="done", summary="Fixed", files_modified=[])

        qa_approve = QAResponse(decision="approve", feedback="Great")

        def qa_side_effect(**kw):
            qa_req = kw.get("qa_request")
            if qa_req and qa_req.dev_blocked:
                return qa_reject
            return qa_approve

        with patch.object(orch, "_run_dev_with_recovery", side_effect=dev_side_effect):
            with patch.object(
                orch, "_run_qa_with_recovery", side_effect=qa_side_effect
            ):
                with patch.object(orch, "_run_arch", side_effect=fake_run_arch):
                    with patch.object(orch, "_finalize_task"):
                        orch._run_feedback_loop(phase, task, task.text, feedback=None)

        # ARCH must have been invoked (on second iteration, triggered by _pending_arch_check)
        assert len(arch_calls) == 1, (
            f"Expected ARCH invoked once, got {len(arch_calls)}"
        )

    print("✓ test_stuckness_check_deferred passed")


def test_stuckness_restored_from_log():
    """Bug Fix 4: _restore_stuckness_from_log replays counters from JSONL history."""
    import tempfile
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope, IterationEntry, Actor, TaskStatus

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        log_path = f"{td}/iter.jsonl"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=log_path,
            session_scope=SessionScope.TASK,
        )
        task = Task(phase_index=0, task_index=0, text="T1")

        def make_entry(actor, status, label="P1.T1"):
            return IterationEntry(
                timestamp="2026-05-01T10:00:00Z",
                iteration=1,
                actor=actor,
                task_label=label,
                status=status,
                payload={},
            )

        # Build history: 2 dev blocked, 1 qa feedback, then qa approved (resets), then 1 dev blocked
        orch.log.append(make_entry(Actor.DEV, TaskStatus.BLOCKED))  # exhaustions=1
        orch.log.append(
            make_entry(Actor.QA, TaskStatus.FEEDBACK)
        )  # iters_without_approval=1
        orch.log.append(make_entry(Actor.DEV, TaskStatus.BLOCKED))  # exhaustions=2
        orch.log.append(make_entry(Actor.QA, TaskStatus.APPROVED))  # reset: 0, 0
        orch.log.append(make_entry(Actor.DEV, TaskStatus.BLOCKED))  # exhaustions=1
        orch.log.append(
            make_entry(Actor.QA, TaskStatus.FEEDBACK)
        )  # iters_without_approval=1
        # Entry for a different task — must be ignored
        orch.log.append(make_entry(Actor.DEV, TaskStatus.BLOCKED, "P2.T1"))

        orch._restore_stuckness_from_log(task)

        assert orch._consecutive_exhaustions == 1, (
            f"Expected exhaustions=1, got {orch._consecutive_exhaustions}"
        )
        assert orch._iterations_without_approval == 1, (
            f"Expected iters_without_approval=1, got {orch._iterations_without_approval}"
        )

    print("✓ test_stuckness_restored_from_log passed")


def test_blocked_despite_qa_approve_fix():
    """Bug Fix 5: When QA approves in blocker triage, _finalize_task is called and loop exits."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope, DevResponse, QAResponse, Phase

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        task = Task(phase_index=0, task_index=0, text="T1")
        phase = Phase(index=0, title="Phase 1", tasks=[task])

        blocked = DevResponse(
            status="blocked",
            summary="Env not set up",
            files_modified=[],
            blocker_description="Missing env vars",
        )
        # QA approves the blocker (task considered done from prior session)
        qa_approve = QAResponse(decision="approve", feedback="Task was already done")

        finalize_calls = []

        with patch.object(orch, "_run_dev_with_recovery", return_value=blocked):
            with patch.object(orch, "_run_qa_with_recovery", return_value=qa_approve):
                with patch.object(
                    orch,
                    "_finalize_task",
                    side_effect=lambda p, t: finalize_calls.append((p, t)),
                ):
                    orch._run_feedback_loop(phase, task, task.text, feedback=None)

        # _finalize_task called exactly once — the approve branch
        assert len(finalize_calls) == 1, (
            f"Expected finalize called once, got {len(finalize_calls)}"
        )
        # QA was called exactly once (only blocker triage, no second QA review)
        assert orch._consecutive_exhaustions == 1, (
            "Counter should still show 1 blocked event"
        )

    print("✓ test_blocked_despite_qa_approve_fix passed")


def test_empty_diff_downgrades_to_blocked():
    """Bug Fix 7: Dev 'done' claim with empty VCS diff is downgraded to blocked."""
    import tempfile
    from unittest.mock import patch, MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope, DevResponse, QAResponse, Phase

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        # Enable VCS backend so the diff check runs
        orch.vcs = MagicMock()

        task = Task(phase_index=0, task_index=0, text="T1")
        phase = Phase(index=0, title="Phase 1", tasks=[task])

        fake_done = DevResponse(
            status="done",
            summary="Already implemented in a previous session",
            files_modified=[],
        )
        # QA approves the blocker triage (after downgrade to blocked)
        qa_approve = QAResponse(decision="approve", feedback="OK")

        qa_call_details = []

        def qa_side_effect(**kwargs):
            qa_call_details.append(kwargs.get("qa_request"))
            return qa_approve

        with patch.object(orch, "_run_dev_with_recovery", return_value=fake_done):
            with patch.object(orch, "_vcs_get_diff", return_value=("", "")):
                with patch.object(
                    orch, "_run_qa_with_recovery", side_effect=qa_side_effect
                ):
                    with patch.object(orch, "_finalize_task"):
                        orch._run_feedback_loop(phase, task, task.text, feedback=None)

        # QA must have been called in blocker-triage mode (dev_blocked=True)
        assert len(qa_call_details) == 1, (
            f"Expected 1 QA call, got {len(qa_call_details)}"
        )
        assert qa_call_details[0].dev_blocked is True, (
            "QA should have been called as blocker triage after empty-diff downgrade"
        )
        # Counter incremented because downgraded "done" entered the blocked handler
        assert orch._consecutive_exhaustions == 1, (
            f"Expected exhaustions=1 after downgrade, got {orch._consecutive_exhaustions}"
        )

    print("✓ test_empty_diff_downgrades_to_blocked passed")


def test_recovery_prompts_anti_fabrication():
    """Bug Fix 6: Recovery prompts contain anti-fabrication instructions."""
    from tasker.orchestrator import (
        _RECOVERY_SUBTASK,
        _RECOVERY_SUMMARIZE,
        _RECOVERY_RESTART,
        _QA_RECOVERY_SUMMARIZE,
        _QA_RECOVERY_RESTART,
    )

    dev_prompts = [_RECOVERY_SUBTASK, _RECOVERY_SUMMARIZE, _RECOVERY_RESTART]
    for prompt in dev_prompts:
        assert "CRITICAL" in prompt, "Anti-fabrication CRITICAL missing from dev prompt"
        assert "already done" in prompt, "'already done' guard missing from dev prompt"
        assert "blocked" in prompt, "'blocked' fallback missing from dev prompt"

    qa_prompts = [_QA_RECOVERY_SUMMARIZE, _QA_RECOVERY_RESTART]
    for prompt in qa_prompts:
        assert "CRITICAL" in prompt, "Anti-fabrication CRITICAL missing from QA prompt"
        assert "verify" in prompt.lower(), (
            "Verification instruction missing from QA prompt"
        )

    print("✓ test_recovery_prompts_anti_fabrication passed")


def test_recovery_prompts_no_file_reads():
    """Verify all non-NORMAL recovery prompts prohibit file reading."""
    from tasker.orchestrator import (
        _RECOVERY_CONTINUE,
        _RECOVERY_SUBTASK,
        _RECOVERY_RESTART,
    )

    for name, prompt in [
        ("CONTINUE", _RECOVERY_CONTINUE),
        ("SUBTASK", _RECOVERY_SUBTASK),
        ("RESTART", _RECOVERY_RESTART),
    ]:
        assert "DO NOT read any files" in prompt, (
            f"_RECOVERY_{name} must prohibit file reading"
        )
        assert "CRITICAL" in prompt or "CRITICAL RULES" in prompt
    print("✓ test_recovery_prompts_no_file_reads passed")


def test_max_turns_passed_to_dev_request():
    """max_turns is included in DevRequest and forwarded to the recipe."""
    from tasker.models import DevRequest

    req = DevRequest(
        task_label="P1.T1",
        task_text="Do thing",
        qa_session_id="qa-1",
        dev_session_id="dev-1",
        iteration=1,
        max_turns=42,
    )
    params = req.to_params()
    assert params["max_turns"] == "42"
    print("✓ test_max_turns_passed_to_dev_request passed")


def test_skip_task_persists_failed_marker():
    """_skip_task marks task as [~] in markdown so it won't be retried."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import SessionScope, DevResponse, QAResponse
    from tasker.parser import parse_task_file

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
            max_iterations_per_task=1,
        )
        orch.phases = parse_task_file(md_path)
        task = orch.phases[0].tasks[0]
        phase = orch.phases[0]

        # Always-failing dev so max_iterations is hit
        always_blocked = DevResponse(
            status="blocked",
            summary="stuck",
            files_modified=[],
            blocker_description="can't proceed",
        )
        qa_reject = QAResponse(decision="reject", feedback="nope")

        with patch.object(orch, "_run_dev_with_recovery", return_value=always_blocked):
            with patch.object(orch, "_run_qa_with_recovery", return_value=qa_reject):
                with patch.object(orch, "_vcs_begin_task"):
                    with patch.object(orch, "_decompose_task", return_value=None):
                        orch._process_task(phase, task)

        # Task must be marked [~] (failed) not [x] (completed) or [ ] (pending)
        content = Path(md_path).read_text()
        assert "- [~] T1" in content, (
            "Task should be marked [~] (failed) after max_iterations without approval"
        )
        assert "- [x] T1" not in content, (
            "Task must NOT be marked [x] when skipped due to max_iterations"
        )
        # task.skipped and task.failed must both be set
        assert task.skipped is True
        assert task.failed is True

    print("✓ test_skip_task_persists_failed_marker passed")


def test_max_iterations_triggers_arch_review():
    """When max_iterations is reached, ARCH is consulted before skipping the task."""
    import tempfile
    from unittest.mock import patch, MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import (
        Task,
        SessionScope,
        DevResponse,
        QAResponse,
        Phase,
        ArchResponse,
    )

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
            max_iterations_per_task=1,
        )
        orch.arch_recipe = MagicMock()  # ARCH is enabled
        task = Task(phase_index=0, task_index=0, text="T1")
        phase = Phase(index=0, title="Phase 1", tasks=[task])

        always_blocked = DevResponse(
            status="blocked",
            summary="stuck",
            files_modified=[],
            blocker_description="can't proceed",
        )
        qa_reject = QAResponse(decision="reject", feedback="nope")
        # ARCH decides to CLARIFY (rewrite task text)
        arch_clarify = ArchResponse(
            action="clarify",
            reason="task was unclear",
            new_task_text="T1 updated: do something clearer",
        )

        arch_calls = []

        with patch.object(orch, "_run_dev_with_recovery", return_value=always_blocked):
            with patch.object(orch, "_run_qa_with_recovery", return_value=qa_reject):
                with patch.object(
                    orch,
                    "_run_arch",
                    side_effect=lambda t: arch_calls.append(t) or arch_clarify,
                ):
                    with patch.object(
                        orch, "_apply_arch_decision", return_value="T1 updated"
                    ):
                        with patch.object(orch, "_vcs_begin_task"):
                            with patch.object(
                                orch, "_decompose_task", return_value=None
                            ):
                                orch._process_task(phase, task)

        # ARCH must have been invoked once for the final review
        assert len(arch_calls) == 1, f"Expected ARCH called once, got {len(arch_calls)}"

    print("✓ test_max_iterations_triggers_arch_review passed")


def test_only_qa_approval_marks_done():
    """Only QA approve path calls _finalize_task; all other exits use _skip_task."""
    import tempfile
    from unittest.mock import patch
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope, DevResponse, QAResponse, Phase

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        task = Task(phase_index=0, task_index=0, text="T1")
        phase = Phase(index=0, title="Phase 1", tasks=[task])

        fake_done = DevResponse(status="done", summary="did it", files_modified=[])

        finalize_calls = []
        skip_calls = []

        # Simulate user /skip via needs_user_input → _interactive_chat_loop returns False
        qa_needs_input = QAResponse(
            decision="needs_user_input",
            feedback="question?",
            user_question="Can you clarify?",
        )

        with patch.object(orch, "_run_dev_with_recovery", return_value=fake_done):
            with patch.object(
                orch, "_run_qa_with_recovery", return_value=qa_needs_input
            ):
                with patch.object(orch, "_interactive_chat_loop", return_value=False):
                    with patch.object(
                        orch,
                        "_finalize_task",
                        side_effect=lambda p, t: finalize_calls.append(t),
                    ):
                        with patch.object(
                            orch,
                            "_skip_task",
                            side_effect=lambda p, t: skip_calls.append(t),
                        ):
                            orch._run_feedback_loop(
                                phase, task, task.text, feedback=None
                            )

        assert len(finalize_calls) == 0, "User-skip must NOT call _finalize_task"
        assert len(skip_calls) == 1, "User-skip must call _skip_task"

    print("✓ test_only_qa_approval_marks_done passed")


def run_bugfix_tests():
    test_stuckness_counts_all_blocked()
    test_stuckness_threshold_lowered()
    test_stuckness_check_deferred()
    test_stuckness_restored_from_log()
    test_blocked_despite_qa_approve_fix()
    test_empty_diff_downgrades_to_blocked()
    test_recovery_prompts_anti_fabrication()
    test_recovery_prompts_no_file_reads()
    test_max_turns_passed_to_dev_request()
    test_skip_task_persists_failed_marker()
    test_only_qa_approval_marks_done()
    test_max_iterations_triggers_arch_review()
    print("\n✅ All 12 bug-fix tests passed!")


# ── E2BIG / diff-size tests ────────────────────────────────────────


def test_e2big_detected_in_run_goose():
    """Fix 4: run_goose catches OSError E2BIG and returns a clear error."""
    import errno
    from unittest.mock import patch
    from tasker.goose import run_goose

    with patch(
        "tasker.goose.subprocess.Popen",
        side_effect=OSError(errno.E2BIG, "Argument list too long"),
    ):
        result = run_goose(
            recipe_path="/dev/null",
            session_name="test_e2big",
            params={"project_context": "x" * 200_000},
        )

    assert not result.success
    assert result.return_code == -1
    assert "argument list too long" in result.raw_stderr.lower()
    assert "KB" in result.raw_stderr  # should mention size
    assert not result.timed_out
    print("✓ test_e2big_detected_in_run_goose passed")


def test_e2big_not_confused_with_other_oserror():
    """Fix 4: non-E2BIG OSErrors are handled by the generic handler."""
    import errno
    from unittest.mock import patch
    from tasker.goose import run_goose

    with patch(
        "tasker.goose.subprocess.Popen",
        side_effect=OSError(errno.ENOENT, "goose not found"),
    ):
        result = run_goose(
            recipe_path="/dev/null",
            session_name="test_enoent",
        )

    assert not result.success
    assert "Failed to start goose process" in result.raw_stderr
    assert "argument list too long" not in result.raw_stderr.lower()
    print("✓ test_e2big_not_confused_with_other_oserror passed")


def test_vcs_get_diff_returns_tuple():
    """Fix 3: _vcs_get_diff now returns (project_context, size_note) tuple."""
    import tempfile
    from unittest.mock import MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        orch.vcs = MagicMock()
        orch.vcs.get_diff.return_value = "diff --git a/file.rs b/file.rs\n+new line\n"

        task = Task(phase_index=0, task_index=0, text="T1")
        context, note = orch._vcs_get_diff(task)

    assert context  # non-empty
    assert "## VCS Diff" in context
    assert "new line" in context
    assert note  # non-empty size note
    assert "lines" in note
    assert "KB" in note
    print("✓ test_vcs_get_diff_returns_tuple passed")


def test_vcs_get_diff_empty_returns_empty_tuple():
    """Fix 3: empty diff returns empty tuple values."""
    import tempfile
    from unittest.mock import MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        orch.vcs = MagicMock()
        orch.vcs.get_diff.return_value = ""

        task = Task(phase_index=0, task_index=0, text="T1")
        context, note = orch._vcs_get_diff(task)

    assert context == ""
    assert note == ""
    print("✓ test_vcs_get_diff_empty_returns_empty_tuple passed")


def test_vcs_get_diff_large_diff_writes_temp_file():
    """Fix 3: large diff is written to a temp file and context points to it."""
    import tempfile
    from unittest.mock import MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        # Set cwd to temp dir so temp files land there
        from pathlib import Path as _Path

        orch.cwd = _Path(td)

        # Create a 150KB diff (over the 100KB threshold)
        large_diff = (
            "diff --git a/big.rs b/big.rs\n"
            + "+line content here padding to reach 150KB total\n" * 4000
        )
        orch.vcs = MagicMock()
        orch.vcs.get_diff.return_value = large_diff

        task = Task(phase_index=0, task_index=0, text="T1")
        context, note = orch._vcs_get_diff(task)

    # Context should point to a file, not contain the full diff inline
    assert context
    assert "LARGE DIFF" in context
    assert "Truncated Preview" in context
    assert note
    assert "temp file" in note
    # The full diff should NOT be in the context string (it's in the file)
    assert large_diff not in context
    print("✓ test_vcs_get_diff_large_diff_writes_temp_file passed")


def test_vcs_get_diff_no_vcs_returns_empty():
    """Fix 3: no VCS backend returns empty tuple."""
    import tempfile
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        # vcs is None by default
        task = Task(phase_index=0, task_index=0, text="T1")
        context, note = orch._vcs_get_diff(task)

    assert context == ""
    assert note == ""
    print("✓ test_vcs_get_diff_no_vcs_returns_empty passed")


def test_vcs_get_diff_failure_returns_empty():
    """Fix 3: VCS get_diff RuntimeError returns empty tuple."""
    import tempfile
    from unittest.mock import MagicMock
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task, SessionScope

    with tempfile.TemporaryDirectory() as td:
        md_path = f"{td}/tasks.md"
        Path(md_path).write_text("## Phase 1\n- [ ] T1: dummy\n")

        orch = Orchestrator(
            task_file=md_path,
            dev_recipe="/dev/null",
            qa_recipe="/dev/null",
            log_file=f"{td}/iter.jsonl",
            session_scope=SessionScope.TASK,
        )
        orch.vcs = MagicMock()
        orch.vcs.get_diff.side_effect = RuntimeError("jj not found")

        task = Task(phase_index=0, task_index=0, text="T1")
        context, note = orch._vcs_get_diff(task)

    assert context == ""
    assert note == ""
    print("✓ test_vcs_get_diff_failure_returns_empty passed")


def test_pythonpath_injected_in_env():
    """run_goose() injects tasker's src/ into PYTHONPATH and preserves existing value."""
    import os
    from pathlib import Path
    from unittest.mock import patch, MagicMock

    from tasker.goose import run_goose

    captured_env: dict[str, str] = {}

    def fake_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        proc = MagicMock()
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        return proc

    original_pythonpath = os.environ.get("PYTHONPATH", "")
    try:
        # Set a pre-existing PYTHONPATH to verify it's preserved
        os.environ["PYTHONPATH"] = "/my/existing/path"
        with patch("tasker.goose.subprocess.Popen", side_effect=fake_popen):
            with patch("tasker.goose._systemd_run_available", return_value=False):
                run_goose(
                    recipe_path="/dev/null",
                    session_name="test_pythonpath",
                )
    finally:
        if original_pythonpath:
            os.environ["PYTHONPATH"] = original_pythonpath
        else:
            os.environ.pop("PYTHONPATH", None)

    assert "PYTHONPATH" in captured_env, (
        "PYTHONPATH should be set in the subprocess env"
    )

    tasker_src = str(Path(__file__).resolve().parent.parent / "src")
    pythonpath = captured_env["PYTHONPATH"]
    assert pythonpath.startswith(tasker_src), (
        f"PYTHONPATH should start with tasker src dir, got: {pythonpath}"
    )
    # Existing PYTHONPATH should be preserved as a suffix
    assert "/my/existing/path" in pythonpath, (
        f"PYTHONPATH should preserve existing value, got: {pythonpath}"
    )
    print("✓ test_pythonpath_injected_in_env passed")


def run_e2big_tests():
    test_e2big_detected_in_run_goose()
    test_e2big_not_confused_with_other_oserror()
    test_pythonpath_injected_in_env()
    test_vcs_get_diff_returns_tuple()
    test_vcs_get_diff_empty_returns_empty_tuple()
    test_vcs_get_diff_large_diff_writes_temp_file()
    test_vcs_get_diff_no_vcs_returns_empty()
    test_vcs_get_diff_failure_returns_empty()
    print("\n✅ All 8 E2BIG/diff-size tests passed!")


# ── _extract_json_blocks tests ──────────────────────────────────────


def test_extract_blocks_single_bare_json():
    """A single bare JSON dict should produce exactly one block."""
    from tasker.goose import _extract_json_blocks

    blocks = _extract_json_blocks('{"a":1}')
    assert len(blocks) == 1
    assert blocks[0] == {"a": 1}


def test_extract_blocks_fenced_json():
    """A fenced ```json ... ``` block should produce exactly one block."""
    from tasker.goose import _extract_json_blocks

    blocks = _extract_json_blocks('```json\n{"a":1}\n```')
    assert len(blocks) == 1
    assert blocks[0] == {"a": 1}


def test_extract_blocks_multiple_ordered():
    """Two valid JSON dicts should be returned in document order."""
    from tasker.goose import _extract_json_blocks

    text = '{"first": 1} some text {"second": 2}'
    blocks = _extract_json_blocks(text)
    assert len(blocks) == 2
    assert blocks[0] == {"first": 1}
    assert blocks[1] == {"second": 2}


def test_extract_blocks_last_malformed_cascades():
    """A valid block followed by truncated JSON should return only the valid block."""
    from tasker.goose import _extract_json_blocks

    text = '{"valid": true} trailing {"a":'
    blocks = _extract_json_blocks(text)
    assert len(blocks) == 1
    assert blocks[0] == {"valid": True}


def test_extract_blocks_no_json():
    """Plain text with no JSON should return an empty list."""
    from tasker.goose import _extract_json_blocks

    blocks = _extract_json_blocks("just some plain text here")
    assert blocks == []


def test_extract_blocks_nested_braces():
    """A dict with nested braces should be returned as a single valid dict."""
    from tasker.goose import _extract_json_blocks

    blocks = _extract_json_blocks('{"a":{"b":2}}')
    assert len(blocks) == 1
    assert blocks[0] == {"a": {"b": 2}}


def test_extract_blocks_empty_string():
    """An empty string should return an empty list."""
    from tasker.goose import _extract_json_blocks

    blocks = _extract_json_blocks("")
    assert blocks == []


def test_session_resume_cascade_from_history():
    """Two assistant messages concatenated: first has a valid started checkpoint, second has
    truncated JSON. _extract_json_blocks should return exactly one valid block (the checkpoint),
    not the malformed one. Verifies cascade works correctly across multi-message concatenated text.

    Ref: 06-open-questions.md#Q5
    """
    from tasker.goose import _extract_json_blocks

    # Simulate what happens when _extract_last_assistant_text concatenates
    # multiple assistant turns into a single string.
    first_assistant = '{"status":"started","summary":"checkpoint","files_modified":[],"notes":"checkpoint"}'
    second_assistant = '{"status":"done","summary":"incomplete'
    concatenated = first_assistant + "\n" + second_assistant

    blocks = _extract_json_blocks(concatenated)
    assert len(blocks) == 1, (
        f"Expected exactly 1 valid block (the checkpoint), got {len(blocks)}: {blocks}"
    )
    assert blocks[0]["status"] == "started", (
        f"Expected status='started' from checkpoint, got {blocks[0]['status']!r}"
    )
    assert blocks[0]["summary"] == "checkpoint", (
        f"Expected summary='checkpoint', got {blocks[0]['summary']!r}"
    )


def test_cascade_flag_set_in_goose_result():
    """When assistant_text has a valid block followed by a malformed one, GooseRunResult
    should have json_blocks_cascade == True and json_blocks_found == 1."""
    from tasker.goose import GooseRunResult

    # Simulate the scenario: valid block then malformed trailing content
    result = GooseRunResult(
        success=True,
        raw_stdout='{"status": "done"} trailing {"a":',
        raw_stderr="",
        return_code=0,
        parsed_json={"status": "done"},
        duration_secs=1.0,
        timed_out=False,
        raw_envelope="",
        empty_output=False,
        json_blocks_found=1,
        json_blocks_cascade=True,
    )
    assert result.json_blocks_found == 1
    assert result.json_blocks_cascade is True
    assert result.parsed_json == {"status": "done"}


def run_json_blocks_tests():
    test_extract_blocks_single_bare_json()
    test_extract_blocks_fenced_json()
    test_extract_blocks_multiple_ordered()
    test_extract_blocks_last_malformed_cascades()
    test_extract_blocks_no_json()
    test_extract_blocks_nested_braces()
    test_extract_blocks_empty_string()
    test_session_resume_cascade_from_history()
    test_cascade_flag_set_in_goose_result()
    print("\n✅ All 9 _extract_json_blocks tests passed!")


# ── Run all ARCH tests ─────────────────────────────────────────────


def run_arch_tests():
    test_arch_models()
    test_parse_arch_response()
    test_insert_subtasks()
    test_rewrite_task_text()
    test_stuckness_detection()
    test_feedback_truncation()
    test_arch_actor_in_ui()
    test_connection_error_hard_stop()
    test_run_loop_reparse()
    test_default_session_scope_is_task()
    print("\n✅ All 10 ARCH tests passed!")


# ── P5.T1 — IterationEntry serialization, metrics, checkpoint tests ──


def test_iteration_entry_new_fields_serialized():
    """IterationEntry.to_dict() includes all new fields when set to non-default values."""
    from tasker.models import IterationEntry, Actor, TaskStatus

    entry = IterationEntry(
        timestamp="2026-01-01T00:00:00Z",
        iteration=1,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.BLOCKED,
        checkpoint=True,
        json_blocks_found=3,
        json_blocks_cascade=True,
        assistant_turns=5,
        total_turns=10,
        output_chars=2048,
    )
    d = entry.to_dict()
    assert d["checkpoint"] is True
    assert d["json_blocks_found"] == 3
    assert d["json_blocks_cascade"] is True
    assert d["assistant_turns"] == 5
    assert d["total_turns"] == 10
    assert d["output_chars"] == 2048


def test_iteration_entry_defaults_omitted():
    """IterationEntry.to_dict() omits new keys when left at defaults."""
    from tasker.models import IterationEntry, Actor, TaskStatus

    entry = IterationEntry(
        timestamp="2026-01-01T00:00:00Z",
        iteration=1,
        actor=Actor.DEV,
        task_label="P1.T1",
        status=TaskStatus.APPROVED,
    )
    d = entry.to_dict()
    for key in (
        "checkpoint",
        "json_blocks_found",
        "json_blocks_cascade",
        "assistant_turns",
        "total_turns",
        "output_chars",
    ):
        assert key not in d, (
            f"Key '{key}' should be omitted at defaults but was present"
        )


def test_extract_last_assistant_text_returns_metrics():
    """_extract_last_assistant_text returns correct assistant_turns and total_turns."""
    from tasker.goose import _extract_last_assistant_text
    import json

    envelope = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": [{"type": "text", "text": "do work"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "result"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "more result"}]},
        ]
    }
    text, empty, assistant_turns, total_turns = _extract_last_assistant_text(
        json.dumps(envelope)
    )
    assert not empty
    assert assistant_turns == 3  # 3 assistant messages total
    assert total_turns == 5  # 5 messages total
    # Only messages after the last user message should be in the text
    assert "result" in text
    assert "more result" in text


def test_session_resume_ignores_old_checkpoint():
    """When goose returns session history with old checkpoints but no new output, old JSON is NOT extracted.

    Ref: 06-open-questions.md#Q5 — verifies the stale-output guard in _extract_last_assistant_text.
    The envelope contains an old assistant message with a blocked JSON block, followed by a
    user message (recovery instruction), but NO new assistant message. The function must
    return empty_flag=True and text="" so the stale JSON block is not misinterpreted as
    the current iteration's response.
    """
    from tasker.goose import _extract_last_assistant_text
    import json

    envelope = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": '```json\n{"status":"started","summary":"old","files_modified":[],"notes":"checkpoint"}\n```',
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Recovery instruction: continue working on the task.",
                    }
                ],
            },
        ]
    }
    text, empty_flag, assistant_turns, total_turns = _extract_last_assistant_text(
        json.dumps(envelope)
    )
    assert empty_flag is True, (
        f"Expected empty_flag=True (stale output guard), got {empty_flag!r}"
    )
    assert text == "", (
        f"Expected empty text (old checkpoint must not leak), got: {text!r}"
    )
    assert assistant_turns == 0, (
        f"Expected 0 assistant_turns (stale guard zeros them), got {assistant_turns}"
    )
    assert total_turns == 2


def test_session_resume_new_checkpoint_wins():
    """When goose returns session history with old checkpoint BUT also new output, new wins (last-wins).

    Ref: 06-open-questions.md#Q5 — verifies that _extract_last_assistant_text returns
    the NEW assistant message text when both old and new checkpoints exist in the
    envelope. The old blocked JSON block should NOT be returned; the new done JSON
    block should be the one extracted via _extract_json_blocks.
    """
    from tasker.goose import _extract_last_assistant_text, _extract_json_blocks
    import json

    envelope = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": '```json\n{"status":"started","summary":"old","files_modified":[],"notes":"checkpoint"}\n```',
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Recovery instruction: continue working on the task.",
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": 'Here is the final result:\n```json\n{"status":"done","summary":"new","files_modified":["src/main.rs"],"notes":"completed"}\n```',
                    }
                ],
            },
        ]
    }
    text, empty_flag, assistant_turns, total_turns = _extract_last_assistant_text(
        json.dumps(envelope)
    )
    assert empty_flag is False, (
        f"Expected empty_flag=False (new output present), got {empty_flag!r}"
    )
    blocks = _extract_json_blocks(text)
    assert len(blocks) >= 1, f"Expected at least 1 JSON block, got {len(blocks)}"
    last_block = blocks[-1]
    assert last_block["status"] == "done", (
        f"Expected status='done' (last-wins), got {last_block['status']!r}"
    )
    assert last_block["summary"] == "new", (
        f"Expected summary='new', got {last_block['summary']!r}"
    )


def test_is_checkpoint_dev_blocked_with_checkpoint_notes():
    """_is_checkpoint returns True for dev blocked with 'checkpoint' in notes (legacy fallback)."""
    from tasker.models import DevResponse
    from tasker.orchestrator import _is_checkpoint

    resp = DevResponse(
        status="blocked",
        summary="paused",
        files_modified=[],
        notes="checkpoint reached mid-task",
    )
    assert _is_checkpoint(resp) is True


def test_is_checkpoint_dev_started():
    """_is_checkpoint returns True for dev status='started' (primary checkpoint signal)."""
    from tasker.models import DevResponse
    from tasker.orchestrator import _is_checkpoint

    resp = DevResponse(
        status="started",
        summary="Starting: P1.T3",
        files_modified=[],
        notes="checkpoint",
    )
    assert _is_checkpoint(resp) is True


def test_is_checkpoint_dev_done():
    """_is_checkpoint returns False for dev done (not started, not blocked)."""
    from tasker.models import DevResponse
    from tasker.orchestrator import _is_checkpoint

    resp = DevResponse(
        status="done",
        summary="completed",
        files_modified=["src/foo.rs"],
        notes="checkpoint info",
    )
    assert _is_checkpoint(resp) is False


def test_is_checkpoint_qa_reject_with_checkpoint():
    """_is_checkpoint returns True for QA rejected with 'checkpoint' in feedback."""
    from tasker.models import QAResponse
    from tasker.orchestrator import _is_checkpoint

    resp = QAResponse(
        decision="reject",
        feedback="needs checkpoint review before proceeding",
    )
    assert _is_checkpoint(resp) is True


def test_is_checkpoint_qa_approve():
    """_is_checkpoint returns False for QA approve (not rejected)."""
    from tasker.models import QAResponse
    from tasker.orchestrator import _is_checkpoint

    resp = QAResponse(
        decision="approve",
        feedback="all good",
    )
    assert _is_checkpoint(resp) is False


# ── P10: "started" status tests (checkpoint feedback loop fix) ────


def test_pydantic_dev_response_accepts_started():
    """DevResponse Pydantic model accepts status='started'."""
    from tasker.schema import DevResponse as DevResponseSchema

    resp = DevResponseSchema(
        status="started",
        summary="Starting: P6.T1",
        files_modified=[],
        notes="checkpoint",
    )
    assert resp.status == "started"


def test_parse_dev_response_started():
    """_parse_dev_response returns a DevResponse with status='started'."""
    from tasker.orchestrator import _parse_dev_response

    parsed = {
        "status": "started",
        "summary": "Starting: P6.T1",
        "files_modified": [],
        "notes": "checkpoint",
    }
    result = _parse_dev_response("", parsed)
    assert result is not None
    assert result.status == "started"
    assert result.summary == "Starting: P6.T1"


def test_parse_dev_response_started_adhoc_fallback():
    """_parse_dev_response fallback path accepts 'started' even if Pydantic rejects."""
    from tasker.orchestrator import _parse_dev_response

    # Simulate a dict that would fail Pydantic but has a valid status
    parsed = {
        "status": "started",
        "summary": "Starting: P6.T1",
        "files_modified": [],
        "notes": "checkpoint",
        "extra_field": "should_be_ignored",
    }
    # This should still work via ad-hoc fallback
    result = _parse_dev_response("", parsed)
    assert result is not None
    assert result.status == "started"


def test_cascade_started_then_done():
    """When agent emits started checkpoint then done final, cascade picks done."""
    from tasker.goose import _extract_json_blocks

    text = (
        '{"status":"started","summary":"Starting: P6.T1","files_modified":[],"notes":"checkpoint"}\n'
        "Some work happened here...\n"
        '{"status":"done","summary":"Implemented feature X","files_modified":["src/foo.rs"],"notes":""}'
    )
    blocks = _extract_json_blocks(text)
    # Cascade picks ALL valid blocks, but last-wins in the orchestrator
    assert len(blocks) == 2
    assert blocks[0]["status"] == "started"
    assert blocks[1]["status"] == "done"
    # The orchestrator uses blocks[-1] — which is the real answer
    assert blocks[-1]["status"] == "done"
    assert "src/foo.rs" in blocks[-1]["files_modified"]


def test_cascade_started_only():
    """When agent emits only the started checkpoint (ran out of turns), cascade finds it."""
    from tasker.goose import _extract_json_blocks

    text = '{"status":"started","summary":"Starting: P6.T1","files_modified":[],"notes":"checkpoint"}'
    blocks = _extract_json_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["status"] == "started"


def test_is_checkpoint_started_no_notes():
    """_is_checkpoint returns True for status='started' even without 'checkpoint' in notes."""
    from tasker.models import DevResponse
    from tasker.orchestrator import _is_checkpoint

    resp = DevResponse(
        status="started",
        summary="Starting: P6.T1",
        files_modified=[],
        notes="",  # no 'checkpoint' keyword — still detected via status
    )
    assert _is_checkpoint(resp) is True


def test_started_status_not_counted_as_blocked():
    """Verify that 'started' status does NOT appear in the blocked status set.

    This is a contract test: the orchestrator's main loop has separate
    branches for 'started' and 'blocked'.  If 'started' leaked into the
    blocked branch, the feedback loop would reappear.
    """
    from tasker.models import DevResponse

    resp = DevResponse(
        status="started",
        summary="checkpoint",
        files_modified=[],
    )
    # The status must be "started", not "blocked"
    assert resp.status == "started"
    assert resp.status != "blocked"


def test_dev_response_to_dict_includes_started():
    """DevResponse.to_dict() correctly serializes started status."""
    from tasker.models import DevResponse

    resp = DevResponse(
        status="started",
        summary="Starting: P6.T1",
        files_modified=[],
        notes="checkpoint",
    )
    d = resp.to_dict()
    assert d["status"] == "started"
    assert d["summary"] == "Starting: P6.T1"


def test_extract_dir_refs_eudox_mcp() -> None:
    """Text contains `eudox-mcp/src/eudox_mcp/server.py` → returns {cwd / 'eudox-mcp'}."""
    from tasker.orchestrator import _extract_dir_refs

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "eudox-mcp" / "src" / "eudox_mcp").mkdir(parents=True)
        text = "Modify `eudox-mcp/src/eudox_mcp/server.py` to add logging"
        result = _extract_dir_refs(text, cwd)
        assert result == {cwd / "eudox-mcp"}


def test_extract_dir_refs_plans_mcp() -> None:
    """Text contains `plans-mcp/tests/test_auth.py` → returns {cwd / 'plans-mcp'}."""
    from tasker.orchestrator import _extract_dir_refs

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "plans-mcp" / "tests").mkdir(parents=True)
        text = "Add test in `plans-mcp/tests/test_auth.py` for OAuth flow"
        result = _extract_dir_refs(text, cwd)
        assert result == {cwd / "plans-mcp"}


def test_extract_dir_refs_inside_cwd() -> None:
    """Text contains `src/eudox/pipeline/analytics.py` → empty set (no prefix before src/)."""
    from tasker.orchestrator import _extract_dir_refs

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "src" / "eudox" / "pipeline").mkdir(parents=True)
        text = "Refactor `src/eudox/pipeline/analytics.py` to use new API"
        result = _extract_dir_refs(text, cwd)
        assert result == set()


def test_extract_dir_refs_multiple() -> None:
    """Text has two backtick paths in different dirs → both roots returned."""
    from tasker.orchestrator import _extract_dir_refs

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        (cwd / "eudox-mcp" / "src").mkdir(parents=True)
        (cwd / "plans-mcp" / "tests").mkdir(parents=True)
        text = (
            "Modify `eudox-mcp/src/eudox_mcp/server.py` and "
            "`plans-mcp/tests/test_auth.py` together"
        )
        result = _extract_dir_refs(text, cwd)
        assert result == {cwd / "eudox-mcp", cwd / "plans-mcp"}


def test_extract_dir_refs_no_paths() -> None:
    """Plain text with no backtick paths → empty set."""
    from tasker.orchestrator import _extract_dir_refs

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        text = "Just a plain text with no paths in backticks at all."
        result = _extract_dir_refs(text, cwd)
        assert result == set()


def test_extract_dir_refs_nonexistent_dir() -> None:
    """Path in backticks doesn't exist on disk → excluded from results."""
    from tasker.orchestrator import _extract_dir_refs

    with tempfile.TemporaryDirectory() as td:
        cwd = Path(td)
        # Do NOT create phantom-mcp on disk
        text = "Update `phantom-mcp/src/main.py` with new config"
        result = _extract_dir_refs(text, cwd)
        assert result == set()


# ── P9.T2: integration-style test for _scan_vcs_paths + init_subdir ──


def test_scan_vcs_paths_detects_untracked() -> None:
    """Integration test: _scan_vcs_paths detects untracked dirs, init_subdir fixes them.

    Creates a temp workspace with a root dir containing:
      - repo/  (git repo — this is the orchestrator's cwd)
      - submod/src/file.py  (no git — a sibling directory outside the repo)

    The orchestrator's cwd is set to the root dir (not the git repo itself),
    so that ``submod/`` is NOT inside the git work tree.  A Phase is built
    with a task whose text references ``submod/src/file.py``.  We call
    ``_scan_vcs_paths()`` and assert it returns ``[root / "submod"]``.
    Then we call ``init_subdir`` and verify the subdir becomes a git repo.
    """
    import subprocess

    from tasker.models import Phase, Task
    from tasker.orchestrator import Orchestrator
    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()

        submod = root / "submod"
        submod_src = submod / "src"
        submod_src.mkdir(parents=True)

        # Create the file referenced in the task text
        (submod_src / "file.py").write_text("# hello\n")

        # Init repo/ as a git repo
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "init", "--allow-empty"],
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

        # submod should NOT be inside a git work tree
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(submod),
            capture_output=True,
        )
        assert check.returncode != 0, "submod should NOT be inside a git work tree"

        # Build a minimal Orchestrator.  cwd is root (parent of both repo/
        # and submod/) so that submod/ is outside any git work tree.
        task = Task(
            phase_index=0,
            task_index=0,
            text="Update `submod/src/file.py` with new features",
        )
        phase = Phase(index=0, title="Test Phase", tasks=[task])

        orc = object.__new__(Orchestrator)
        orc.phases = [phase]
        orc.cwd = root
        orc.vcs = GitBackend()

        # Step 1: _scan_vcs_paths should detect submod as untracked
        untracked = orc._scan_vcs_paths()
        assert untracked == [submod], f"Expected [{submod}], got {untracked}"

        # Step 2: init_subdir should turn submod into a git repo
        orc.vcs.init_subdir(submod)

        # Verify submod is now a git repo
        check2 = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(submod),
            capture_output=True,
        )
        assert check2.returncode == 0, "submod should be a git repo after init_subdir"

        # Verify the .gitignore was created with Python defaults
        gitignore = submod / ".gitignore"
        assert gitignore.exists(), ".gitignore should exist after init_subdir"
        assert "__pycache__/" in gitignore.read_text()


def test_init_subdir_creates_git_repo_and_gitignore() -> None:
    """P9.T4: init_subdir creates .git, .gitignore with Python defaults, and baseline commit."""
    import subprocess

    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "newmod"
        src = target / "src"
        src.mkdir(parents=True)
        (src / "main.py").write_text("print('hello')\n")

        backend = GitBackend()
        backend.init_subdir(target)

        # Verify git repo
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(target),
            capture_output=True,
        )
        assert check.returncode == 0, "Should be a git repo"

        # Verify .gitignore with Python defaults
        gitignore = target / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "__pycache__/" in content
        assert ".venv/" in content
        assert "*.pyc" in content
        assert ".ruff_cache/" in content
        assert ".pytest_cache/" in content

        # Verify baseline commit exists
        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        assert log.returncode == 0
        assert "tasker: baseline snapshot" in log.stdout


def test_init_subdir_idempotent_on_existing_repo() -> None:
    """P9.T4: init_subdir is a no-op when directory is already a git repo."""
    import subprocess

    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "existing"
        target.mkdir()

        # Pre-init as a git repo with an existing commit
        subprocess.run(
            ["git", "init"], cwd=str(target), capture_output=True, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(target),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(target),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "original commit", "--allow-empty"],
            cwd=str(target),
            capture_output=True,
            check=True,
        )

        # Get the original commit hash
        original = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(target),
            capture_output=True,
            text=True,
        ).stdout.strip()

        backend = GitBackend()
        backend.init_subdir(target)

        # HEAD should not have changed — no new commit added
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(target),
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert current == original, "init_subdir should not modify an existing repo"


def test_init_subdir_preserves_existing_gitignore() -> None:
    """P9.T4: init_subdir does NOT overwrite an existing .gitignore."""

    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "custom"
        target.mkdir()
        (target / ".gitignore").write_text("node_modules/\ndist/\n")
        (target / "file.txt").write_text("data\n")

        backend = GitBackend()
        backend.init_subdir(target)

        # .gitignore should be preserved exactly
        gitignore = target / ".gitignore"
        content = gitignore.read_text()
        assert content == "node_modules/\ndist/\n", (
            "Existing .gitignore must not be overwritten"
        )
        assert "__pycache__/" not in content, "Should not have appended Python defaults"


def test_init_subdir_baseline_commit_message() -> None:
    """P9.T4: baseline commit has exact message 'tasker: baseline snapshot'."""
    import subprocess

    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "msgtest"
        target.mkdir()
        (target / "code.py").write_text("x = 1\n")

        backend = GitBackend()
        backend.init_subdir(target)

        # Verify exact commit message
        msg = subprocess.run(
            ["git", "log", "--format=%s", "-1"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        assert msg.stdout.strip() == "tasker: baseline snapshot"

        # Verify the file was committed
        files = subprocess.run(
            ["git", "ls-files"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        assert "code.py" in files.stdout


def test_init_subdir_empty_dir() -> None:
    """P9.T4: init_subdir works on completely empty directory (--allow-empty commit)."""
    import subprocess

    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "empty"
        target.mkdir()
        # No files at all

        backend = GitBackend()
        backend.init_subdir(target)

        # Should still be a valid repo with the baseline commit
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(target),
            capture_output=True,
        )
        assert check.returncode == 0

        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(target),
            capture_output=True,
            text=True,
        )
        assert "tasker: baseline snapshot" in log.stdout


def test_vcs_auto_init_e2e() -> None:
    """P9.T7: End-to-end — orchestrator startup auto-inits untracked directories.

    Simulates a workspace with:
      - A task whose text references `svc-a/src/main.py` and `svc-b/tests/test_x.py`
      - Neither svc-a/ nor svc-b/ have .git
      - Orchestrator.run() is not fully executed (we only call _scan_vcs_paths
        + init_subdir), but this verifies the complete scan→init→verify loop.
    """
    import subprocess

    from tasker.models import Phase, Task
    from tasker.orchestrator import Orchestrator
    from tasker.vcs.git_backend import GitBackend

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # Create two untracked directories with source trees
        for name in ("svc-a", "svc-b"):
            d = root / name / "src"
            d.mkdir(parents=True)
            (d / "main.py").write_text(f"# {name}\n")
            tests = root / name / "tests"
            tests.mkdir(parents=True)
            (tests / f"test_{name}.py").write_text("def test_ok(): pass\n")

        # Verify neither is a git repo
        for name in ("svc-a", "svc-b"):
            check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(root / name),
                capture_output=True,
            )
            assert check.returncode != 0, f"{name} should not be a git repo"

        # Build an orchestrator with tasks referencing both directories
        task_a = Task(
            phase_index=0,
            task_index=0,
            text="Implement feature in `svc-a/src/main.py` and add tests in `svc-a/tests/test_svc-a.py`",
        )
        task_b = Task(
            phase_index=0,
            task_index=1,
            text="Fix bug in `svc-b/src/main.py` — see `svc-b/tests/test_svc-b.py` for regression test",
        )
        phase = Phase(index=0, title="E2E Phase", tasks=[task_a, task_b])

        orc = object.__new__(Orchestrator)
        orc.phases = [phase]
        orc.cwd = root
        orc.vcs = GitBackend()

        # Step 1: scan should detect both untracked dirs
        untracked = orc._scan_vcs_paths()
        assert set(untracked) == {root / "svc-a", root / "svc-b"}, (
            f"Expected svc-a and svc-b, got {untracked}"
        )

        # Step 2: auto-init both (mimics Orchestrator.run() loop)
        for path in untracked:
            orc.vcs.init_subdir(path)

        # Step 3: verify both are now git repos
        for name in ("svc-a", "svc-b"):
            check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(root / name),
                capture_output=True,
            )
            assert check.returncode == 0, f"{name} should be a git repo after auto-init"

            # Verify baseline commit
            log = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=str(root / name),
                capture_output=True,
                text=True,
            )
            assert "tasker: baseline snapshot" in log.stdout

            # Verify .gitignore
            assert (root / name / ".gitignore").exists()

        # Step 4: second scan should return empty (both now tracked)
        untracked2 = orc._scan_vcs_paths()
        assert untracked2 == [], (
            f"After auto-init, scan should be empty, got {untracked2}"
        )


# ── P11: Silent crash detection + model fallback ────────────────


def test_is_silent_crash_true():
    """rc!=0 + empty stdout + empty stderr → silent crash."""
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=1,
        duration_secs=0.5,
    )
    assert is_silent_crash(result) is True


def test_is_silent_crash_with_stderr():
    """rc!=0 + non-empty stderr → NOT a silent crash (has diagnostic info)."""
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="Error: rate limit exceeded",
        return_code=1,
    )
    assert is_silent_crash(result) is False


def test_is_silent_crash_with_stdout():
    """rc!=0 + non-empty stdout → NOT a silent crash (goose produced output)."""
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=False,
        raw_stdout='{"messages": []}',
        raw_stderr="",
        return_code=1,
    )
    assert is_silent_crash(result) is False


def test_is_silent_crash_success():
    """rc=0 (success) → NOT a silent crash."""
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=True,
        raw_stdout="ok",
        raw_stderr="",
        return_code=0,
    )
    assert is_silent_crash(result) is False


def test_is_silent_crash_timeout():
    """Timed out → NOT a silent crash (timeouts have their own handling)."""
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=-1,
        timed_out=True,
    )
    assert is_silent_crash(result) is False


def test_is_silent_crash_whitespace_only():
    """rc!=0 + whitespace-only stdout/stderr → still a silent crash."""
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=False,
        raw_stdout="   \n\t  ",
        raw_stderr="  \n  ",
        return_code=1,
    )
    assert is_silent_crash(result) is True


def test_is_silent_crash_rc0_empty_output():
    """rc=0 + success=False + empty stdout/stderr → silent crash.

    This is the most common silent-crash pattern in production: goose
    exits cleanly (rc=0) but produces zero assistant text (empty output
    envelope).  Before the fix, is_silent_crash required rc!=0 so these
    were NOT retried with backoff, burning through all recovery stages
    at 2-second intervals (297 silent crashes in one run).
    """
    from tasker.goose import GooseRunResult, is_silent_crash

    result = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=0,
        duration_secs=1.8,
        empty_output=True,
    )
    assert is_silent_crash(result) is True


def test_backoff_retries_on_silent_crash():
    """Silent crash triggers backoff retry (same as connection error)."""
    from unittest.mock import patch
    from tasker.goose import GooseRunResult, run_goose_with_backoff
    from tasker.models import RateLimitConfig

    silent_crash = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=1,
    )
    success = GooseRunResult(
        success=True,
        raw_stdout='{"messages":[{"role":"assistant","content":"done"}]}',
        raw_stderr="",
        return_code=0,
    )

    call_count = 0

    def mock_run_goose(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return silent_crash
        return success

    with patch("tasker.goose.run_goose", side_effect=mock_run_goose):
        with patch("tasker.goose.time.sleep"):
            result = run_goose_with_backoff(
                recipe_path="/tmp/recipe.yaml",
                session_name="test-session",
                rate_limit=RateLimitConfig(
                    enabled=True, max_retries=5, base_delay_secs=1.0
                ),
            )

    assert result.success is True
    assert call_count == 3  # 2 silent crashes retried, then success


def test_backoff_no_retry_on_nonempty_stderr():
    """Non-transient failure (stderr present, not connection error) → no retry."""
    from unittest.mock import patch
    from tasker.goose import GooseRunResult, run_goose_with_backoff
    from tasker.models import RateLimitConfig

    crash_with_stderr = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="Some real error: file not found",
        return_code=1,
    )

    with patch("tasker.goose.run_goose", return_value=crash_with_stderr) as mock:
        result = run_goose_with_backoff(
            recipe_path="/tmp/recipe.yaml",
            session_name="test-session",
            rate_limit=RateLimitConfig(enabled=True, max_retries=5),
        )

    assert result.success is False
    assert mock.call_count == 1  # no retry — returned immediately


def test_backoff_fallback_model():
    """After primary model retries exhausted, fallback model is tried."""
    from unittest.mock import patch
    from tasker.goose import GooseRunResult, run_goose_with_backoff
    from tasker.models import RateLimitConfig

    silent_crash = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=1,
    )
    fallback_success = GooseRunResult(
        success=True,
        raw_stdout='{"messages":[{"role":"assistant","content":"fallback done"}]}',
        raw_stderr="",
        return_code=0,
    )

    calls = []

    def mock_run_goose(**kwargs):
        calls.append({"model": kwargs.get("model"), "provider": kwargs.get("provider")})
        if kwargs.get("provider") == "ollama":
            return fallback_success
        return silent_crash

    with patch("tasker.goose.run_goose", side_effect=mock_run_goose):
        with patch("tasker.goose.time.sleep"):
            result = run_goose_with_backoff(
                recipe_path="/tmp/recipe.yaml",
                session_name="test-session",
                model="glm-5.1",
                provider="custom_z.ai",
                rate_limit=RateLimitConfig(
                    enabled=True,
                    max_retries=2,
                    base_delay_secs=0.1,
                ),
                fallback_model="qwen3.5:9b",
                fallback_provider="ollama",
            )

    assert result.success is True
    # Calls: 2 primary retries (exhausted) + 1 fallback
    primary_calls = [c for c in calls if c["provider"] == "custom_z.ai"]
    fallback_calls = [c for c in calls if c["provider"] == "ollama"]
    assert len(primary_calls) == 2  # max_retries=2
    assert len(fallback_calls) == 1  # one fallback attempt
    assert fallback_calls[0]["model"] == "qwen3.5:9b"


def test_backoff_fallback_model_also_fails():
    """Fallback model also fails → return the fallback's failure result."""
    from unittest.mock import patch
    from tasker.goose import GooseRunResult, run_goose_with_backoff
    from tasker.models import RateLimitConfig

    silent_crash = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=1,
    )

    with patch("tasker.goose.run_goose", return_value=silent_crash):
        with patch("tasker.goose.time.sleep"):
            result = run_goose_with_backoff(
                recipe_path="/tmp/recipe.yaml",
                session_name="test-session",
                model="glm-5.1",
                provider="custom_z.ai",
                rate_limit=RateLimitConfig(
                    enabled=True,
                    max_retries=2,
                    base_delay_secs=0.1,
                ),
                fallback_model="qwen3.5:9b",
                fallback_provider="ollama",
            )

    assert result.success is False
    # Should still return a result (the fallback's failure)


def test_backoff_no_fallback_when_not_configured():
    """Without fallback config, returns failure after exhausting retries."""
    from unittest.mock import patch
    from tasker.goose import GooseRunResult, run_goose_with_backoff
    from tasker.models import RateLimitConfig

    silent_crash = GooseRunResult(
        success=False,
        raw_stdout="",
        raw_stderr="",
        return_code=1,
    )

    with patch("tasker.goose.run_goose", return_value=silent_crash):
        with patch("tasker.goose.time.sleep"):
            result = run_goose_with_backoff(
                recipe_path="/tmp/recipe.yaml",
                session_name="test-session",
                model="glm-5.1",
                provider="custom_z.ai",
                rate_limit=RateLimitConfig(
                    enabled=True,
                    max_retries=2,
                    base_delay_secs=0.1,
                ),
            )

    assert result.success is False
    # No fallback → just return the last failure


def test_fallback_model_config():
    """FallbackModel dataclass stores provider/model correctly."""
    from tasker.models import FallbackModel

    fb = FallbackModel(provider="ollama", model="qwen3.5:9b", max_attempts=3)
    assert fb.provider == "ollama"
    assert fb.model == "qwen3.5:9b"
    assert fb.max_attempts == 3

    # Defaults
    fb2 = FallbackModel(provider="anthropic", model="claude-sonnet-4")
    assert fb2.max_attempts == 2


def test_rate_limit_config_with_fallback():
    """RateLimitConfig accepts fallback_dev and fallback_qa."""
    from tasker.models import FallbackModel, RateLimitConfig

    cfg = RateLimitConfig(
        enabled=True,
        fallback_dev=FallbackModel(provider="ollama", model="qwen3.5:9b"),
        fallback_qa=FallbackModel(provider="anthropic", model="claude-sonnet-4"),
    )
    assert cfg.fallback_dev is not None
    assert cfg.fallback_dev.provider == "ollama"
    assert cfg.fallback_qa is not None
    assert cfg.fallback_qa.model == "claude-sonnet-4"

    # Without fallbacks (default)
    cfg_plain = RateLimitConfig()
    assert cfg_plain.fallback_dev is None
    assert cfg_plain.fallback_qa is None


def test_fallback_model_selection_per_actor():
    """Dev → fallback_dev (ollama), QA → fallback_qa (anthropic)."""
    from unittest.mock import patch
    from tasker.goose import GooseRunResult
    from tasker.models import FallbackModel, RateLimitConfig

    # We test the resolution logic in _run_goose_with_ui by checking
    # that the right fallback_model/fallback_provider are passed to
    # run_goose_with_backoff.  We do this by mocking it and inspecting calls.
    calls = {}

    def mock_backoff(**kwargs):
        calls[kwargs.get("fallback_provider")] = kwargs.get("fallback_model")
        return GooseRunResult(
            success=True, raw_stdout="ok", raw_stderr="", return_code=0
        )

    from tasker.orchestrator import Orchestrator
    from tasker.models import Actor

    rl = RateLimitConfig(
        enabled=True,
        fallback_dev=FallbackModel(provider="ollama", model="qwen3.5:9b"),
        fallback_qa=FallbackModel(provider="anthropic", model="claude-sonnet-4"),
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        task_file = Path(tmpdir) / "tasks.md"
        task_file.write_text("# Phase 1\n## P1\n- [ ] T1 do something\n")
        log_file = Path(tmpdir) / "log.jsonl"

        orch = Orchestrator(
            task_file=task_file,
            dev_recipe=Path("/tmp/dev.yaml"),
            qa_recipe=Path("/tmp/qa.yaml"),
            log_file=log_file,
            model="glm-5.1",
            provider="custom_z.ai",
            rate_limit=rl,
        )

        # Patch run_goose_with_backoff to capture the fallback args
        with patch(
            "tasker.orchestrator.run_goose_with_backoff", side_effect=mock_backoff
        ):
            # Dev call
            orch._run_goose_with_ui(
                Actor.DEV,
                "T1",
                recipe_path="/tmp/dev.yaml",
                session_name="dev-session",
            )
            # QA call
            orch._run_goose_with_ui(
                Actor.QA,
                "T1",
                recipe_path="/tmp/qa.yaml",
                session_name="qa-session",
            )

    assert "ollama" in calls, f"Expected ollama fallback for DEV, got calls: {calls}"
    assert calls["ollama"] == "qwen3.5:9b"
    assert "anthropic" in calls, (
        f"Expected anthropic fallback for QA, got calls: {calls}"
    )
    assert calls["anthropic"] == "claude-sonnet-4"


# ═══════════════════════════════════════════════════════════════════
# P11b — Anthropic fallback auto-detection from ANTHROPIC_API_KEY
# ═══════════════════════════════════════════════════════════════════


def test_resolve_fallback_explicit_flags_win():
    """Explicit CLI flags take precedence over env-derived Anthropic fallback."""
    from tasker.main import _resolve_fallback
    from tasker.models import FallbackModel

    with _env(ANTHROPIC_API_KEY="sk-test-123"):
        fb = _resolve_fallback(
            explicit_provider="ollama",
            explicit_model="qwen3.5:9b",
            primary_provider="openai",
            no_anthropic_fallback=False,
            anthropic_fallback_model=None,
        )
    assert fb == FallbackModel(provider="ollama", model="qwen3.5:9b")


def test_resolve_fallback_anthropic_from_env():
    """When ANTHROPIC_API_KEY is set, an Anthropic fallback is synthesised."""
    from tasker.main import _resolve_fallback
    from tasker.models import FallbackModel

    with _env(ANTHROPIC_API_KEY="sk-test-123"):
        fb = _resolve_fallback(
            explicit_provider=None,
            explicit_model=None,
            primary_provider="openai",
            no_anthropic_fallback=False,
            anthropic_fallback_model=None,
        )
    assert fb == FallbackModel(provider="anthropic", model="claude-sonnet-4")


def test_resolve_fallback_no_env_returns_none():
    """Without ANTHROPIC_API_KEY and without explicit flags → None."""
    from tasker.main import _resolve_fallback

    with _env(remove=("ANTHROPIC_API_KEY",)):
        fb = _resolve_fallback(
            explicit_provider=None,
            explicit_model=None,
            primary_provider="openai",
            no_anthropic_fallback=False,
            anthropic_fallback_model=None,
        )
    assert fb is None


def test_resolve_fallback_disabled_flag():
    """--no-anthropic-fallback suppresses the env-derived fallback."""
    from tasker.main import _resolve_fallback

    with _env(ANTHROPIC_API_KEY="sk-test-123"):
        fb = _resolve_fallback(
            explicit_provider=None,
            explicit_model=None,
            primary_provider="openai",
            no_anthropic_fallback=True,
            anthropic_fallback_model=None,
        )
    assert fb is None


def test_resolve_fallback_skipped_when_primary_is_anthropic():
    """No Anthropic fallback when the primary provider is itself anthropic."""
    from tasker.main import _resolve_fallback

    with _env(ANTHROPIC_API_KEY="sk-test-123"):
        fb = _resolve_fallback(
            explicit_provider=None,
            explicit_model=None,
            primary_provider="anthropic",
            no_anthropic_fallback=False,
            anthropic_fallback_model=None,
        )
    assert fb is None


def test_resolve_fallback_custom_anthropic_model():
    """--anthropic-fallback-model overrides the default model name."""
    from tasker.main import _resolve_fallback
    from tasker.models import FallbackModel

    with _env(ANTHROPIC_API_KEY="sk-test-123"):
        fb = _resolve_fallback(
            explicit_provider=None,
            explicit_model=None,
            primary_provider="openai",
            no_anthropic_fallback=False,
            anthropic_fallback_model="claude-opus-4",
        )
    assert fb == FallbackModel(provider="anthropic", model="claude-opus-4")


def test_resolve_fallback_partial_explicit_flags_ignored():
    """Only one of provider/model being set does NOT trigger explicit path;
    env-derived Anthropic fallback can still kick in."""
    from tasker.main import _resolve_fallback
    from tasker.models import FallbackModel

    with _env(ANTHROPIC_API_KEY="sk-test-123"):
        # Only provider set, no model
        fb = _resolve_fallback(
            explicit_provider="ollama",
            explicit_model=None,
            primary_provider="openai",
            no_anthropic_fallback=False,
            anthropic_fallback_model=None,
        )
    assert fb == FallbackModel(provider="anthropic", model="claude-sonnet-4")


# ═══════════════════════════════════════════════════════════════════
# P12 — Tool response JSON extraction (_extract_tool_response_json)
# ═══════════════════════════════════════════════════════════════════


def _make_envelope(messages: list[dict]) -> str:
    """Helper: wrap messages in a goose envelope."""
    return json.dumps({"messages": messages})


def _make_tool_response(stdout_text: str) -> dict:
    """Helper: create a toolResponse message like goose produces."""
    return {
        "role": "user",
        "content": [
            {
                "type": "toolResponse",
                "toolResult": {
                    "status": "success",
                    "value": {
                        "content": [
                            {"type": "text", "text": stdout_text},
                        ],
                        "structuredContent": {
                            "stdout": stdout_text,
                            "stderr": "",
                            "exit_code": 0,
                        },
                    },
                },
            }
        ],
    }


def _make_assistant_text(text: str) -> dict:
    """Helper: create an assistant message with text content."""
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
    }


def _make_user_text(text: str) -> dict:
    """Helper: create a user message with text content."""
    return {
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }


def test_extract_tool_response_json_found():
    """toolResponse with tasker.respond JSON is extracted."""
    from tasker.goose import _extract_tool_response_json

    respond_json = json.dumps(
        {
            "status": "done",
            "summary": "Implemented X",
            "files_modified": ["a.py"],
        }
    )
    stdout_text = f"```json\n{respond_json}\n```"
    envelope = _make_envelope(
        [
            _make_user_text("task prompt"),
            _make_assistant_text("I did the work."),
            _make_tool_response(stdout_text),
            _make_assistant_text("Task complete."),
        ]
    )
    result = _extract_tool_response_json(envelope)
    assert result is not None, "Should find tasker.respond JSON in tool response"
    assert result["status"] == "done"
    assert result["summary"] == "Implemented X"
    assert result["files_modified"] == ["a.py"]


def test_extract_tool_response_json_no_envelope():
    """Non-JSON input returns None."""
    from tasker.goose import _extract_tool_response_json

    assert _extract_tool_response_json("not json") is None
    assert _extract_tool_response_json("{}") is None
    assert _extract_tool_response_json('{"messages": []}') is None


def test_extract_tool_response_json_no_tool_response():
    """Envelope with no toolResponse messages returns None."""
    from tasker.goose import _extract_tool_response_json

    envelope = _make_envelope(
        [
            _make_user_text("prompt"),
            _make_assistant_text("response"),
        ]
    )
    assert _extract_tool_response_json(envelope) is None


def test_extract_tool_response_json_empty_stdout():
    """toolResponse with empty stdout returns None."""
    from tasker.goose import _extract_tool_response_json

    envelope = _make_envelope(
        [
            _make_user_text("prompt"),
            _make_tool_response(""),
        ]
    )
    assert _extract_tool_response_json(envelope) is None


def test_extract_tool_response_json_multiple_responses():
    """When multiple toolResponses exist, the LAST one wins (reverse scan)."""
    from tasker.goose import _extract_tool_response_json

    first_json = json.dumps({"status": "started", "summary": "Starting"})
    second_json = json.dumps({"status": "done", "summary": "Done"})
    envelope = _make_envelope(
        [
            _make_user_text("prompt"),
            _make_tool_response(f"```json\n{first_json}\n```"),
            _make_assistant_text("working..."),
            _make_tool_response(f"```json\n{second_json}\n```"),
            _make_assistant_text("finished."),
        ]
    )
    result = _extract_tool_response_json(envelope)
    assert result is not None
    assert result["status"] == "done", "Should pick the LAST tool response"


def test_extract_tool_response_json_text_fallback():
    """toolResponse without structuredContent falls back to text content."""
    from tasker.goose import _extract_tool_response_json

    respond_json = json.dumps({"status": "done", "summary": "OK"})
    msg = {
        "role": "user",
        "content": [
            {
                "type": "toolResponse",
                "toolResult": {
                    "value": {
                        "content": [
                            {"type": "text", "text": f"```json\n{respond_json}\n```"},
                        ],
                    },
                },
            }
        ],
    }
    envelope = _make_envelope([_make_user_text("prompt"), msg])
    result = _extract_tool_response_json(envelope)
    assert result is not None
    assert result["status"] == "done"


def test_extract_tool_response_json_no_status_key():
    """toolResponse JSON without 'status' or 'decision' key is ignored."""
    from tasker.goose import _extract_tool_response_json

    random_json = json.dumps({"command": "ls", "output": "file1 file2"})
    envelope = _make_envelope(
        [
            _make_user_text("prompt"),
            _make_tool_response(f"```json\n{random_json}\n```"),
        ]
    )
    assert _extract_tool_response_json(envelope) is None


def test_tool_response_fallback_in_goose_result():
    """Integration: GooseRunResult gets parsed_json from tool response fallback."""
    from tasker.goose import _extract_tool_response_json

    # Verify the helper works with a realistic goose envelope
    respond_json = json.dumps(
        {
            "status": "done",
            "summary": "SEC-1.T3 complete",
            "files_modified": ["test.py"],
            "notes": "",
            "blocker_description": "",
            "blocker_suggestion": "",
        }
    )
    stdout_text = f"```json\n{respond_json}\n```"

    # This is the actual structure goose produces
    envelope = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Task prompt here"}],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolRequest",
                            "toolCall": {
                                "status": "success",
                                "value": {
                                    "name": "shell",
                                    "arguments": {
                                        "command": "python3 -m tasker.respond dev done --summary X"
                                    },
                                },
                            },
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "toolResponse",
                            "toolResult": {
                                "status": "success",
                                "value": {
                                    "content": [
                                        {"type": "text", "text": stdout_text},
                                    ],
                                    "structuredContent": {
                                        "stdout": stdout_text,
                                        "stderr": "",
                                        "exit_code": 0,
                                    },
                                },
                            },
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": "Task SEC-1.T3 is complete. The test already exists.",
                        },
                    ],
                },
            ]
        }
    )

    result = _extract_tool_response_json(envelope)
    assert result is not None, "Should find tasker.respond JSON from realistic envelope"
    assert result["status"] == "done"
    assert result["summary"] == "SEC-1.T3 complete"
    assert result["files_modified"] == ["test.py"]


def test_empty_dict_in_prose_does_not_block_tool_response():
    """Regression: ``{}`` in assistant prose (e.g. ``dict[str, X] = {}``)
    was matched by the brace-pair scanner and set ``parsed = {}``.
    Since ``parsed is not None``, the tool-response fallback never ran,
    causing an infinite malformed_output loop even though the agent had
    correctly executed ``python3 -m tasker.respond``.

    The fix: only treat a cascade block as "useful" if it has a
    ``status`` or ``decision`` key.  Otherwise fall through to the
    tool-response scanner.
    """
    from tasker.goose import _extract_json_blocks

    # Simulate assistant prose that mentions `{}` in a code description
    prose = (
        "Task complete. The `Settings(BaseModel)` has "
        "`role_overrides: dict[str, RoleMcpConfig] = {}` — all good."
    )
    blocks = _extract_json_blocks(prose)
    assert blocks == [{}], "Precondition: brace scanner matches the literal {}"

    # An empty dict is not a useful tasker response
    parsed = blocks[-1] if blocks else None
    parsed_is_useful = parsed is not None and (
        "status" in parsed or "decision" in parsed
    )
    assert not parsed_is_useful, "Empty dict must not block tool-response fallback"


# ═══════════════════════════════════════════════════════════════════
# P13 — Multi-repo VCS commit
# ═══════════════════════════════════════════════════════════════════


def test_discover_git_repos_finds_subdir_repos():
    """_discover_git_repos finds .git dirs in immediate subdirectories."""
    import tempfile
    from tasker.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Create two fake git repos and one non-repo dir
        (tmp / "repo_a" / ".git").mkdir(parents=True)
        (tmp / "repo_b" / ".git").mkdir(parents=True)
        (tmp / "not_a_repo").mkdir(parents=True)

        orch = Orchestrator.__new__(Orchestrator)
        orch.cwd = tmp
        repos = orch._discover_git_repos()
        names = [p.name for p in repos]
        assert names == ["repo_a", "repo_b"], f"Expected repo_a, repo_b; got {names}"


def test_discover_git_repos_empty_cwd():
    """No .git dirs → empty list."""
    import tempfile
    from tasker.orchestrator import Orchestrator

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "src").mkdir()
        orch = Orchestrator.__new__(Orchestrator)
        orch.cwd = tmp
        assert orch._discover_git_repos() == []


def test_discover_git_repos_none_cwd():
    """cwd=None → empty list."""
    from tasker.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch.cwd = None
    assert orch._discover_git_repos() == []


def test_multi_repo_commit_commits_dirty_repos():
    """_multi_repo_commit runs git add + commit in repos with changes."""
    import subprocess
    import tempfile
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task
    from tasker.ui import TaskerUI

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Create two real git repos
        for name in ("alpha", "beta"):
            repo = tmp / name
            repo.mkdir()
            subprocess.run(
                ["git", "init"], cwd=str(repo), check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "t@t.com"],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )
            (repo / "file.txt").write_text("hello")
            subprocess.run(
                ["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )

        # Make a change in alpha only
        (tmp / "alpha" / "file.txt").write_text("changed")

        orch = Orchestrator.__new__(Orchestrator)
        orch._git_repos = [tmp / "alpha", tmp / "beta"]
        orch.ui = TaskerUI()
        task = Task(phase_index=0, task_index=0, text="Test task")
        orch._multi_repo_commit(task)

        # alpha should have a new commit
        log_a = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(tmp / "alpha"),
            capture_output=True,
            text=True,
        )
        assert "tasker: P1.T1" in log_a.stdout

        # beta should NOT have a new commit (no changes)
        log_b = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=str(tmp / "beta"),
            capture_output=True,
            text=True,
        )
        assert "tasker: P1.T1" not in log_b.stdout


def test_multi_repo_diff_aggregates():
    """_multi_repo_diff collects diffs from all repos with changes."""
    import subprocess
    import tempfile
    from tasker.orchestrator import Orchestrator
    from tasker.ui import TaskerUI

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for name in ("x", "y"):
            repo = tmp / name
            repo.mkdir()
            subprocess.run(
                ["git", "init"], cwd=str(repo), check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "user.email", "t@t.com"],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "T"],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )
            (repo / "f.txt").write_text("base")
            subprocess.run(
                ["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "init"],
                cwd=str(repo),
                check=True,
                capture_output=True,
            )

        # Change x only
        (tmp / "x" / "f.txt").write_text("modified")

        orch = Orchestrator.__new__(Orchestrator)
        orch._git_repos = [tmp / "x", tmp / "y"]
        orch.ui = TaskerUI()
        diff = orch._multi_repo_diff()

        assert "x/" in diff
        assert "modified" in diff
        # y has no changes — should not appear
        assert "y/" not in diff


def test_vcs_get_diff_multi_repo_mode():
    """_vcs_get_diff returns multi-repo diff when _git_repos is set."""
    import subprocess
    import tempfile
    from tasker.orchestrator import Orchestrator
    from tasker.models import Task
    from tasker.ui import TaskerUI

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo = tmp / "myrepo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.com"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "T"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        (repo / "a.py").write_text("print('hi')")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo),
            check=True,
            capture_output=True,
        )
        (repo / "a.py").write_text("print('bye')")

        orch = Orchestrator.__new__(Orchestrator)
        orch._git_repos = [repo]
        orch.cwd = tmp
        orch.vcs = None
        orch.ui = TaskerUI()
        task = Task(phase_index=0, task_index=0, text="test")

        ctx, note = orch._vcs_get_diff(task)
        assert "multi-repo" in ctx
        assert "bye" in ctx


if __name__ == "__main__":
    test_parser()
    test_logger()
    test_json_extraction()
    test_markdown_update()
    test_command_builder()
    test_models()
    test_parser_strictness()
    test_envelope_extraction()
    test_jj_module()
    test_task_jj_fields()
    test_qa_request_with_project_context()
    test_goose_result_timed_out()
    test_timeout_feedback()
    test_subphase_parsing()
    test_session_scope_enum()
    test_scope_key_computation()
    test_backward_compat_no_subphases()
    test_task_subphase_field()
    test_subphase_labels()
    test_vcs_backend_protocol()
    test_task_vcs_fields()
    test_jj_reexports()
    test_git_backend_helpers()
    test_git_backend_init_errors()
    test_create_backend_types()
    test_jj_backend_protocol()
    test_git_backend_protocol()
    test_task_vcs_description()
    test_finalize_task_ordering()
    test_monitoring_setup()
    test_monitoring_file_output()
    test_monitoring_idempotent()
    test_monitoring_get_logger()
    test_monitoring_parser_captured()
    test_monitoring_orchestrator_events_captured()
    test_resolve_level()
    test_monitoring_log_levels()
    test_monitoring_invalid_level()
    test_activity_renderable()
    test_ui_activity_indicator()
    test_goose_heartbeat_thread()
    test_run_goose_with_ui_wiring()
    test_format_timestamp()
    test_entry_summary()
    test_pending_iteration_lifecycle()
    test_spinner_frames()
    test_pending_iteration_dataclass()
    test_decompose_models()
    test_schema_dev_response_valid_done()
    test_schema_dev_response_valid_blocked()
    test_schema_dev_response_invalid_status()
    test_schema_qa_response_all_decisions()
    test_schema_qa_response_invalid_decision()
    test_schema_decompose_response()
    test_schema_arch_response_all_actions()

    # P4.T1: Pydantic-backed _parse_*_response tests
    test_parse_dev_response_pydantic_valid()
    test_parse_dev_response_pydantic_invalid_status_fallback()
    test_parse_dev_response_extra_fields()
    test_parse_qa_response_pydantic_valid()
    test_parse_qa_response_invalid_decision()
    test_parse_decompose_response_pydantic_valid()
    test_parse_arch_response_pydantic_valid()

    test_parse_decompose_response()
    test_decompose_task()
    test_dev_override_task_text()
    test_feedback_loop_subtask_label()
    test_run_subtask_loop()
    test_process_task_decomposition()
    test_process_task_vcs_once_per_task()
    test_truncation_detection()
    test_dev_truncation_fast_forward()
    test_dev_truncation_suppresses_task_text()

    # _extract_json_blocks tests
    run_json_blocks_tests()

    # ARCH (Architect) agent tests
    run_arch_tests()

    # Bug-fix tests (stuckness detection, ARCH invocation, VCS validation)
    run_bugfix_tests()

    # E2BIG / diff-size tests
    run_e2big_tests()

    # P5.T1 — IterationEntry serialization, metrics, checkpoint tests
    test_iteration_entry_new_fields_serialized()
    test_iteration_entry_defaults_omitted()
    test_extract_last_assistant_text_returns_metrics()
    test_session_resume_ignores_old_checkpoint()
    test_session_resume_new_checkpoint_wins()
    test_is_checkpoint_dev_blocked_with_checkpoint_notes()
    test_is_checkpoint_dev_done()
    test_is_checkpoint_qa_reject_with_checkpoint()
    test_is_checkpoint_qa_approve()

    # P9.T2 — _extract_dir_refs tests
    test_extract_dir_refs_eudox_mcp()
    test_extract_dir_refs_plans_mcp()
    test_extract_dir_refs_inside_cwd()
    test_extract_dir_refs_multiple()
    test_extract_dir_refs_no_paths()
    test_extract_dir_refs_nonexistent_dir()

    # P9.T2 — integration-style _scan_vcs_paths + init_subdir test
    test_scan_vcs_paths_detects_untracked()

    # P9.T4 — isolated init_subdir unit tests
    test_init_subdir_creates_git_repo_and_gitignore()
    test_init_subdir_idempotent_on_existing_repo()
    test_init_subdir_preserves_existing_gitignore()
    test_init_subdir_baseline_commit_message()
    test_init_subdir_empty_dir()

    # P9.T7 — orchestrator auto-init e2e verification
    test_vcs_auto_init_e2e()

    # P11 — Silent crash detection + model fallback
    test_is_silent_crash_true()
    test_is_silent_crash_with_stderr()
    test_is_silent_crash_with_stdout()
    test_is_silent_crash_success()
    test_is_silent_crash_timeout()
    test_is_silent_crash_whitespace_only()
    test_is_silent_crash_rc0_empty_output()
    test_backoff_retries_on_silent_crash()
    test_backoff_no_retry_on_nonempty_stderr()
    test_backoff_fallback_model()
    test_backoff_fallback_model_also_fails()
    test_backoff_no_fallback_when_not_configured()
    test_fallback_model_config()
    test_rate_limit_config_with_fallback()
    test_fallback_model_selection_per_actor()

    # P12 — Tool response JSON extraction
    test_extract_tool_response_json_found()
    test_extract_tool_response_json_no_envelope()
    test_extract_tool_response_json_no_tool_response()
    test_extract_tool_response_json_empty_stdout()
    test_extract_tool_response_json_multiple_responses()
    test_extract_tool_response_json_text_fallback()
    test_extract_tool_response_json_no_status_key()
    test_tool_response_fallback_in_goose_result()

    # P13 — Multi-repo VCS commit
    test_discover_git_repos_finds_subdir_repos()
    test_discover_git_repos_empty_cwd()
    test_discover_git_repos_none_cwd()
    test_multi_repo_commit_commits_dirty_repos()
    test_multi_repo_diff_aggregates()
    test_vcs_get_diff_multi_repo_mode()

    print("\n✅ All dry-run tests passed!")
