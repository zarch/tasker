# 03 — JSON-First Protocol

## Goal

Invert the agent's output strategy: **emit the JSON block first** (as a
tentative "checkpoint"), then do the work, then update it.  Today the JSON
comes last — if the agent runs out of tokens, the orchestrator gets nothing.

---

## 3.1 The core insight

Current recipe pattern:

```
1. Read task
2. Check code (2 turns)
3. Read specs (4 turns)
4. Implement (8 turns)
5. Test (2 turns)
6. Output JSON block  ← runs out of tokens here
```

The JSON block is a **postscript** — it comes after all the work.
If the LLM's output token budget is exhausted at step 5, the entire
turn is wasted.

Proposed pattern:

```
1. Read task → immediately output checkpoint JSON: {"status":"started","summary":"investigating: <task>"}
2. Check code (2 turns)
3. Read specs if needed (4 turns)
4. Implement (8 turns)
5. Output final JSON: {"status":"done","summary":"...","files_modified":[...]}
```

Now even if the agent dies after step 1, the orchestrator has a valid
`blocked` response it can act on — instead of entering recovery.

---

## 3.2 Checkpoint semantics

A **checkpoint** is a valid DevResponse/QAResponse emitted early with
conservative defaults:

### Dev checkpoint

```json
{"status": "started", "summary": "Starting: <task_label>", "files_modified": [], "notes": "checkpoint"}
```

### QA checkpoint

```json
{"decision": "needs_user_input", "feedback": "Review in progress for <task_label>", "concerns": ["checkpoint"]}
```

### Rules

| Rule | Rationale |
|---|---|
| Dev checkpoint uses `status: "started"` | **Not** blocked — orchestrator skips stuckness counter and QA triage. Purely analytics. |
| QA checkpoint uses `decision: "needs_user_input"` | Safe default — won't cascade into reject→feedback→dev loop |
| Only the **last** JSON block in the response is parsed | Backward compatible with cascade extraction |
| Agent may emit multiple checkpoints | Each supersedes the previous; orchestrator sees the final one |
| If agent outputs only a checkpoint and runs out of turns | Orchestrator sees `started` → logs it → retries with recovery guidance (no stuckness penalty) |

---

## 3.3 How this changes the recovery flow

### Today (JSON-last)

```
Agent works... → no JSON → malformed_output
  → recovery stage CONTINUE: "just output JSON" → agent re-does work → maybe JSON
  → recovery stage SUBTASK: "do one tiny piece" → agent starts over → maybe JSON
  ...11 attempts...
```

### With JSON-first (proposed)

```
Agent outputs checkpoint (started) → works → outputs final JSON
  → success (most common path)

Agent outputs checkpoint (started) → works → dies (token exhaustion)
  → orchestrator extracts checkpoint → started DevResponse
  → orchestrator logs "checkpoint_only" → retries with recovery guidance
  → NO stuckness penalty, NO QA triage loop
```

The checkpoint turns a **malformed_output** error into a **started** response,
which the orchestrator handles gracefully: log analytics, emit recovery
feedback, and retry.  No feedback loop.

---

## 3.4 Checkpoint detection and logging (mandatory)

Checkpoint detection is **for analytics only** — it does not change control flow.
Both checkpoint and final JSON are valid DevResponse/QAResponse objects.  The
three paths already exist in the orchestrator:

- A checkpoint with `status: "blocked"` → dev gets feedback, retries → productive
- A final response with `status: "done"` → QA reviews → productive
- A final response with `status: "blocked"` → QA triages → productive

Detection convention (decided in Q2):

```python
def _is_checkpoint(response: DevResponse) -> bool:
    return (
        response.status == "started"
        or (response.status == "blocked" and "checkpoint" in response.notes)
    )
```

When detected, the orchestrator:
1. Emits a `dev.checkpoint` or `qa.checkpoint` log event
2. Sets `checkpoint: True` on the IterationEntry
3. Logs `checkpoint_summary` / `checkpoint_decision` for analytics

**The `started` status is analytics-only** — it does NOT trigger blocked
handling, stuckness counter increment, or QA triage.  The orchestrator's
main dev-QA loop has an explicit `if dev_response.status == "started"` branch
that logs and retries with recovery guidance, bypassing the blocked path entirely.

---

## 3.5 What about sessions and context?

**Problem:** goose sessions accumulate conversation history.  If the agent
emits a checkpoint at turn 1, then works for 15 turns, the final response
contains all 15 turns of assistant text.  The `_extract_last_assistant_text`
function already handles this — it concatenates all assistant messages and
`_extract_json_block` finds the **last** JSON block.

**No change needed.** The last JSON block wins.

---

## 3.6 Interaction with the Pydantic model (spec 02)

The checkpoint pattern works with or without Pydantic:

| Approach | Checkpoint code in agent |
|---|---|
| Raw JSON (today) | `print('{"status":"started","summary":"Starting","files_modified":[]}')` |
| Pydantic (spec 02) | `from tasker.schema import DevResponse; print(DevResponse(status="started",...).model_dump_json())` |

Both produce the same output.  Pydantic guarantees the fields are correct;
raw JSON is simpler but typo-prone.  We can support both.
