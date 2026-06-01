# 05 — Control and Safety

## Goal

The JSON-first protocol and Pydantic models change what the agent emits.
This document specifies how the orchestrator maintains control and ensures
no task is falsely marked complete.

---

## 5.1 Invariants (must never be violated)

| # | Invariant | Enforcement |
|---|---|---|
| INV-1 | A task is marked `[x]` only after QA explicitly decides `approve` | `_finalize_task` called only from QA `approve` branch |
| INV-2 | A task marked `[~]` is never retried in the same run | `find_next_task` skips `task.failed` |
| INV-3 | The orchestrator never trusts a single goose call | Recovery stages + circuit breaker as safety net |
| INV-4 | VCS diff is checked before accepting `done` | Empty diff → downgraded to `blocked` (Bug Fix 7) |
| INV-5 | The last JSON block in the response wins | `_extract_json_block` uses `rfind` / last match |

### How checkpoints interact with invariants

- Checkpoint is `blocked` / `reject` → never triggers `approve` path → **INV-1 safe**
- Checkpoint is a valid JSON block → `_extract_json_block` returns it → **no recovery triggered**
- If agent dies after checkpoint, orchestrator extracts it → normal `blocked` handling → **INV-3 safe**
- Final JSON overwrites checkpoint because it's the last block → **INV-5 guarantees this**

---

## 5.2 New failure modes and mitigations

### Failure: Agent outputs checkpoint but then produces garbage

**Likelihood:** Low. The checkpoint is turn 1, the agent has 99% of its
token budget left for real work.

**Mitigation:** The garbage response won't contain a *later* JSON block,
so `_extract_json_block` finds the checkpoint → `blocked` → normal retry.

### Failure: Agent outputs two conflicting JSON blocks

**Likelihood:** Medium (agent emits checkpoint, then emits final JSON,
but the checkpoint accidentally has a valid `done` status).

**Mitigation:** `_extract_json_block` finds the **last** `{...}` match.
The final block always wins.  And the checkpoint convention uses
`blocked`/`reject`, never `done`/`approve`.

### Failure: Agent uses Pydantic but imports wrong version

**Likelihood:** Low (we pin `pydantic>=2.0` in tasker dependencies).

**Mitigation:** Even if the import fails, the agent falls back to raw JSON
in a code block.  `_extract_json_block` handles both.  No crash possible.

### Failure: PYTHONPATH injection breaks agent's Python environment

**Likelihood:** Low (we prepend, not replace).

**Mitigation:** The PYTHONPATH change only adds the tasker's `src/` directory.
If the agent never imports from it, nothing changes.  If it does, it gets
a clean module with no side effects.

---

## 5.3 Circuit breaker interaction

The circuit breaker (from PR #1) counts **consecutive empty outputs**.
With JSON-first, empty output becomes even rarer (the checkpoint itself
is non-empty).  But the circuit breaker still guards against the structural
goose failure (rc=0, no assistant messages).

```
consecutive_empty counter:
  incremented when:  empty_flag=True from _extract_last_assistant_text
  reset when:        any non-empty output received (including checkpoint)
  triggers when:     counter >= max_consecutive_empty (default 3)
  effect:            break recovery loop, mark task as [~]
```

**No change needed.** The checkpoint produces non-empty output, so it
resets the counter.  The circuit breaker only fires on the 85.5% structural
failures that are unrelated to the JSON-first protocol.

---

## 5.4 Recovery stage changes

With JSON-first, recovery stages trigger **less often** because:

| Scenario | Today | With JSON-first |
|---|---|---|
| Agent works, token limit hit, no JSON | Recovery (11 attempts) | Checkpoint extracted → `blocked` → 1 retry |
| Agent works, outputs final JSON | Success (1 call) | Success (1 call) |
| Agent works, outputs JSON with typo | Recovery | Recovery (same) |
| Agent never starts (empty output) | Circuit breaker (3 calls) | Circuit breaker (3 calls, unchanged) |

### Should we reduce recovery attempts?

**Not yet.** Even with JSON-first, the recovery stages are still needed for:
- JSON with typos (Pydantic would catch these, but raw JSON fallback doesn't)
- Edge cases where the checkpoint is somehow invalid
- Backward compatibility with agents that don't follow JSON-first

We can tune recovery attempts down after measuring the impact in production.

---

## 5.5 Cascade extraction (Q4 decision)

When the agent emits multiple JSON blocks and the **last one is malformed**,
the orchestrator falls back to the previous valid block.  This replaces the
current single-shot `_extract_json_block` with `_extract_json_blocks` (plural).

```python
def _extract_json_blocks(text: str) -> list[dict]:
    """Extract all valid JSON dicts from text, in document order."""
    ...
```

Caller takes `blocks[-1]` (last valid).  If only one block exists and it's
valid, behavior is identical to today.  If multiple blocks exist, the last
valid one wins — even if a later malformed block exists.

Safety relies on existing invariants: QA reviews the VCS diff, empty-diff-done
is downgraded to `blocked`.

## 5.6 Rollout strategy

1. **Phase A — Pydantic models only** (low risk)
   - Add `schema.py`, inject PYTHONPATH
   - Update recipes to mention Pydantic as **recommended** (not yet mandatory)
   - Add `_extract_json_blocks` cascade (backward compatible)
   - Add checkpoint detection + metrics logging
   - No JSON-first checkpoint requirement yet
   - Measure: does Pydantic usage reduce malformed output?

2. **Phase B — JSON-first in recipes** (medium risk)
   - Add **mandatory** checkpoint instructions to recipes (Q1 decision)
   - Keep recovery stages at current levels
   - Add unit tests for session resume with old checkpoints (Q5)
   - Measure: does checkpoint reduce recovery invocations?

3. **Phase C — Tune recovery** (after data)
   - If malformed_output drops below 5% of errors, reduce recovery stages
   - e.g., CONTINUE(1) → SUBTASK(1) → done (3 attempts total instead of 11)

Each phase is independently deployable and measurable.
