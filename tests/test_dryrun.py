"""End-to-end dry-run test — mocks goose subprocess to test the full pipeline."""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Add src to path so we can import tasker
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tasker.parser import parse_task_file, find_next_task, update_markdown
from tasker.log import IterationLog
from tasker.models import IterationEntry, Actor, TaskStatus
from tasker.ui import TaskerUI


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
    text4 = 'Just plain text, no json here'
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
    assert params["recovery_instruction"] == ""  # empty on first iteration (always provided)

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
    assert qa_params_b["blocker_description"] == "No spec defines the config file format"

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
    assert chat_params["conversation_history"] == "👤 User: What API?\n🧪 QA: Not specified"

    # RecoveryStage enum
    from tasker.models import RecoveryStage
    assert RecoveryStage.NORMAL.max_attempts == 3
    assert RecoveryStage.CONTINUE.max_attempts == 3
    assert RecoveryStage.SUBTASK.max_attempts == 3
    assert RecoveryStage.SUMMARIZE.max_attempts == 3

    print("✓ Model tests passed")


# ── 7. Test parser strictness ────────────────────────────────────

def test_parser_strictness():
    """Test that parsers return None on truly unparseable output (no guessing)."""
    from tasker.orchestrator import _parse_dev_response, _parse_qa_response

    # Valid dev response — done
    assert _parse_dev_response(
        '{"status": "done", "summary": "ok", "files_modified": []}',
        {"status": "done", "summary": "ok", "files_modified": []},
    ) is not None

    # Valid dev response — blocked
    resp = _parse_dev_response(
        '{"status": "blocked", "summary": "stuck", "files_modified": [], '
        '"blocker_description": "no spec", "blocker_suggestion": "ask user"}',
        {"status": "blocked", "summary": "stuck", "files_modified": [],
         "blocker_description": "no spec", "blocker_suggestion": "ask user"},
    )
    assert resp is not None
    assert resp.status == "blocked"
    assert resp.blocker_description == "no spec"

    # Invalid dev response — unknown status
    assert _parse_dev_response(
        '{"status": "hmm", "summary": "???"}',
        {"status": "hmm", "summary": "???"},
    ) is None

    # Invalid dev response — no status key at all
    assert _parse_dev_response("just text", None) is None
    assert _parse_dev_response(
        '{"summary": "ok but no status"}',
        {"summary": "ok but no status"},
    ) is None

    # Valid QA response — approve
    assert _parse_qa_response(
        '{"decision": "approve", "feedback": "LGTM"}',
        {"decision": "approve", "feedback": "LGTM"},
    ) is not None

    # Valid QA response — needs_user_input
    resp2 = _parse_qa_response(
        '{"decision": "needs_user_input", "feedback": "need info", "user_question": "what?"}',
        {"decision": "needs_user_input", "feedback": "need info", "user_question": "what?"},
    )
    assert resp2 is not None
    assert resp2.decision == "needs_user_input"
    assert resp2.user_question == "what?"

    # Invalid QA response — unknown decision
    assert _parse_qa_response(
        '{"decision": "maybe", "feedback": "idk"}',
        {"decision": "maybe", "feedback": "idk"},
    ) is None

    # Invalid QA response — no decision key
    assert _parse_qa_response("just text", None) is None

    print("✓ Parser strictness tests passed")


# ── 8. Test envelope extraction ──────────────────────────────────

def test_envelope_extraction():
    """Test that the goose JSON envelope is properly unwrapped."""
    from tasker.goose import _extract_last_assistant_text

    # Valid envelope with assistant message
    envelope = json.dumps({
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "do task"}]},
            {"role": "assistant", "content": [
                {"type": "text", "text": 'I did it\n```json\n{"status": "done", "summary": "ok", "files_modified": []}\n```'}
            ]},
        ]
    })
    result = _extract_last_assistant_text(envelope)
    assert '{"status": "done"' in result
    assert "I did it" in result

    # Envelope with multiple assistant messages
    envelope2 = json.dumps({
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "thinking..."}]},
            {"role": "assistant", "content": [{"type": "text", "text": '{"status": "done"}'}]},
        ]
    })
    result2 = _extract_last_assistant_text(envelope2)
    assert "thinking..." in result2
    assert '{"status": "done"}' in result2

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
        success=True, raw_stdout="ok", raw_stderr="", return_code=0,
    )
    assert result_ok.timed_out is False

    # Explicit timed out
    result_timeout = GooseRunResult(
        success=False, raw_stdout="", raw_stderr="TIMEOUT after 600s",
        return_code=-1, duration_secs=600.5, timed_out=True,
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
        phase_index=0, task_index=2, text="Some task", subphase="P1-2 API Endpoints", subphase_index=0
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
    assert _compute_scope_key(task_no_sub, SessionScope.SUBPHASE) == "P2"  # falls back to phase
    assert _compute_scope_key(task_no_sub, SessionScope.TASK) == "P2::T1"

    # Same scope key for tasks in the same subphase
    task_a = Task(phase_index=0, task_index=0, text="A", subphase="P1-1 DB", subphase_index=0)
    task_b = Task(phase_index=0, task_index=1, text="B", subphase="P1-1 DB", subphase_index=1)
    assert _compute_scope_key(task_a, SessionScope.SUBPHASE) == _compute_scope_key(task_b, SessionScope.SUBPHASE)
    # Different TASK scope keys for different tasks in same subphase
    assert _compute_scope_key(task_a, SessionScope.TASK) != _compute_scope_key(task_b, SessionScope.TASK)

    # Different scope keys for tasks in different subphases
    task_c = Task(phase_index=0, task_index=2, text="C", subphase="P1-2 API", subphase_index=0)
    assert _compute_scope_key(task_a, SessionScope.SUBPHASE) != _compute_scope_key(task_c, SessionScope.SUBPHASE)

    # Different scope keys for tasks in different phases
    task_d = Task(phase_index=1, task_index=0, text="D", subphase="P1-1 DB", subphase_index=0)
    assert _compute_scope_key(task_a, SessionScope.PHASE) != _compute_scope_key(task_d, SessionScope.PHASE)

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

    task2 = Task(phase_index=0, task_index=1, text="Another", subphase="P1-1 Setup", subphase_index=0)
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
        phase_index=0, task_index=24, text="Some pixel task",
        subphase="P1-4 · hay-grid — Pixel", subphase_index=2,
    )
    assert task.label == "P1-4.T3"  # 3rd task (0-based index 2) under this ###

    # First task in a subphase
    task_first = Task(
        phase_index=0, task_index=0, text="Setup DB",
        subphase="P1-1 Database Setup", subphase_index=0,
    )
    assert task_first.label == "P1-1.T1"

    # Second task in the same subphase
    task_second = Task(
        phase_index=0, task_index=1, text="Run migrations",
        subphase="P1-1 Database Setup", subphase_index=1,
    )
    assert task_second.label == "P1-1.T2"

    # Task without subphase — falls back to flat positional label
    task_no_sub = Task(phase_index=2, task_index=5, text="No subphase here")
    assert task_no_sub.label == "P3.T6"

    # Task with subphase="" and subphase_index=-1 — also flat fallback
    task_neg = Task(
        phase_index=1, task_index=3, text="Orphan",
        subphase="", subphase_index=-1,
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
    print("\n✅ All 19 dry-run tests passed!")
