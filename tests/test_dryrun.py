"""End-to-end dry-run test — mocks goose subprocess to test the full pipeline."""

import json
import sys
import tempfile
from pathlib import Path

# Add src to path so we can import tasker
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tasker.parser import parse_task_file, find_next_task, update_markdown
from tasker.log import IterationLog
from tasker.models import IterationEntry, Actor, TaskStatus


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
    from tasker.goose import _extract_json_block

    # Fenced code block
    text1 = 'Some text\n```json\n{"status": "done", "summary": "ok"}\n```\nmore text'
    result1 = _extract_json_block(text1)
    assert result1 == {"status": "done", "summary": "ok"}

    # Raw JSON
    text2 = '{"decision": "approve", "feedback": "LGTM", "concerns": []}'
    result2 = _extract_json_block(text2)
    assert result2 == {"decision": "approve", "feedback": "LGTM", "concerns": []}

    # Last brace fallback
    text3 = 'Blah blah\n{"status": "done"}'
    result3 = _extract_json_block(text3)
    assert result3 == {"status": "done"}

    # No JSON
    text4 = "Just plain text, no json here"
    result4 = _extract_json_block(text4)
    assert result4 is None

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

    # Valid envelope with assistant message
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
    result = _extract_last_assistant_text(envelope)
    assert '{"status": "done"' in result
    assert "I did it" in result

    # Envelope with multiple assistant messages
    envelope2 = json.dumps(
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
    result2 = _extract_last_assistant_text(envelope2)
    assert "thinking..." in result2
    assert '{"status": "done"}' in result2

    # Envelope with only user messages (goose errored before agent responded)
    envelope_no_assistant = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "do task"}]},
            ]
        }
    )
    result_no_asst = _extract_last_assistant_text(envelope_no_assistant)
    assert result_no_asst == "", (
        f"Expected empty string for envelope with no assistant messages, got: {result_no_asst!r}"
    )

    # Empty messages list
    envelope_empty = json.dumps({"messages": []})
    result_empty = _extract_last_assistant_text(envelope_empty)
    assert result_empty == ""

    # Not JSON — fallback to raw
    result3 = _extract_last_assistant_text("plain text output")
    assert result3 == "plain text output"

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
        assert "task.markdown_updated" in content
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
                with patch("tasker.orchestrator.run_goose", return_value=fake_result):
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
    print("\n✅ All 47 dry-run tests passed!")
