# 01 — Current Flow (As-Is)

This document describes the dev-QA loop exactly as it works today,
so we have a shared baseline for discussing changes.

---

## 1. Task lifecycle

```
_process_task(task)
  ├── _decompose_task(task)          ← optional, returns subtasks
  │     └── [for each subtask]:
  │           _run_feedback_loop(task, subtask.text)
  └── _run_feedback_loop(task, task.text)
```

The feedback loop is the core cycle:

```
_run_feedback_loop(task, effective_task_text)
  for iteration 1..max_iterations:
    1. dev_response = _run_dev_with_recovery(task, iteration, feedback)
    2. validate dev claim via VCS diff (done → blocked if empty diff)
    3. if blocked → QA triages blocker
    4. qa_response = _run_qa_with_recovery(task, iteration, dev_response)
    5. switch on qa_response.decision:
         approve   → _finalize_task(), return
         reject    → build feedback, continue loop
         needs_user_input → interactive chat, continue loop
```

---

## 2. Dev call chain

```
_run_dev_with_recovery(task, iteration, feedback)
  └── while True (recovery loop):
        1. Build DevRequest (task_label, task_text, feedback, recovery_instruction)
        2. dev_request.to_params() → dict[str, str]
        3. run_goose(recipe=dev_recipe, session=dev_session, params=...)
        4. goose subprocess runs, returns JSON envelope
        5. _extract_last_assistant_text(stdout) → (text, empty_flag)
        6. if empty_flag → circuit breaker (consecutive_empty++)
        7. _extract_json_block(text) → dict | None
        8. _parse_dev_response(text, parsed) → DevResponse | None
        9. if None → escalate recovery stage:
             NORMAL(1) → CONTINUE(3) → SUBTASK(3) → SUMMARIZE(1) → RESTART(1)
        10. if all stages exhausted → synthetic blocked DevResponse
```

### Recovery stages

| Stage | Attempts | Instruction to agent |
|---|---|---|
| NORMAL | 1 | None (first call) |
| CONTINUE | 3 | "Don't re-investigate, just output the JSON block" |
| SUBTASK | 3 | "Do ONE tiny piece, then output JSON" |
| SUMMARIZE | 1 | "Stop, output blocked JSON with progress summary" |
| RESTART | 1 | "Fresh start, minimal stubs only" |

**Total attempts before giving up: 11**

---

## 3. QA call chain

Same structure as dev but with QARequest → QAResponse:

```
_run_qa_with_recovery(task, iteration, qa_request)
  └── while True (recovery loop):
        1. Build QARequest (task_label, dev_summary, files_modified, ...)
        2. qa_request.to_params() → dict[str, str]
        3. run_goose(recipe=qa_recipe, session=qa_session, params=...)
        4–8. Same extraction pipeline
        9. _parse_qa_response → QAResponse | None
        10. if None → escalate:
              NORMAL(1) → CONTINUE(3) → SUMMARIZE(1) → RESTART(1)
```

**Total QA attempts before giving up: 7**

---

## 4. How the agent sees instructions today

### Recipe template (Jinja2)

The recipe YAML has an `instructions` section (system prompt) and a `prompt`
section (user prompt).  Jinja2 `{{ variables }}` are filled from `--params`.

Example from `recipe-dev.yaml` prompt section:

```
**Task {{ task_label }}**: {{ task_text }}

- Your session ID: `{{ dev_session_id }}`
- Iteration: #{{ iteration }}
{% if feedback %}
## QA Feedback from Previous Iteration
{{ feedback }}
{% endif %}

**CRITICAL — JSON block discipline:**
- Turns 1-2: Quick code check
- If code exists → turns 3-5: verify + output JSON
- If code missing → turns 3-16: read specs + implement. Turn 17+: output JSON
- At turn 35 or later: MUST output JSON NOW
```

### What the agent must produce

Dev agent must end its response with a JSON block:

```json
{"status": "done", "summary": "...", "files_modified": [...], "notes": "..."}
```

or

```json
{"status": "blocked", "summary": "...", "files_modified": [...], "blocker_description": "...", "blocker_suggestion": "..."}
```

QA agent must end with:

```json
{"decision": "approve", "feedback": "...", "concerns": []}
```

or reject / needs_user_input variants.

### How goose delivers the output

`goose run --output-format json --quiet` returns:

```json
{"messages": [
  {"role": "user", "content": [{"type": "text", "text": "..."}]},
  {"role": "assistant", "content": [{"type": "text", "text": "..."}]},
  {"role": "user", "content": [{"type": "text", "text": "..."}]},
  {"role": "assistant", "content": [{"type": "text", "text": "..."}]}
]}
```

The tasker extracts the **last assistant message** (concatenating all
assistant content blocks) and then runs `_extract_json_block` on that text.

---

## 5. Where the 13.9% failure happens

The agent's last assistant message looks like this:

```
The configuration is present and correct. Let me verify it matches
the task requirements precisely...

**Task requirements:**
- ✅ known-first-party = ["eudox"]
- ✅ force-sort-within-sections = true
```

The agent did real work (read files, ran grep), but the response got
**truncated by the LLM's output token limit** before the JSON block
was appended.  The `_extract_json_block` function finds no JSON and
the orchestrator enters recovery.

Key observation: the agent spent its output token budget on
**prose explanation** of what it found, leaving nothing for the
JSON block that the orchestrator needs.
