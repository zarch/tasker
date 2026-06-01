# 06 — Open Questions

## Decisions made

| # | Question | Decision |
|---|---|---|
| Q1 | Mandatory vs encouraged checkpoint? | **Mandatory** — recipe says MUST, orchestrator logs compliance |
| Q2 | Detect/log checkpoints separately? | **Yes** — full analytics logging with checkpoint-specific events |
| Q3 | Schema location? | **`tasker/schema.py`** — no separate package |
| Q4 | Multiple JSON blocks — fallback cascade? | **Yes** — try last, if malformed fall back to previous valid block |
| Q5 | Session resume safety? | **Add unit tests** to verify old checkpoints don't leak |
| Q6 | Reduce turn budget? | **Keep at 100** — recipe deadline tightened to turn 35 |
| Q7 | Skip truncation recovery when checkpoint exists? | **No** — keep truncation recovery as-is |
| Q8 | Metrics? | **Yes** — add to iteration JSONL (see §8 below) |

---

## Q4 — Cascade extraction (detailed design)

Decision: **cascade** — try last block first, if malformed fall back to
the previous valid block.

Example:
1. Turn 1: `{"status":"blocked","summary":"Starting: P0.1.T3"}`
2. Turn 5: `{"status":"done","summary":"Implemented X","files_modified":["a.rs"]}`
3. Turn 10: `{"status":"done","summary":"Also fixed Y","files_modified": ???` ← truncated

With cascade: try turn 10 (malformed) → fall back to turn 5 (valid `done`).

### Safety analysis

| Concern | Why it's not a problem |
|---|---|
| Stale `done` claim (turn 5) but agent kept working | QA reviews **actual VCS diff**, not agent claims |
| False `done` from earlier block | Empty-diff validation (Bug Fix 7) downgrades to `blocked` |
| Stale `blocked` from checkpoint | Dev retries with feedback — productive either way |
| Agent outputs `done` at turn 1 then gets blocked at turn 5 | Turn 5 (malformed) → fallback to turn 1 `done` → QA sees empty diff → `blocked` |

### New function: `_extract_json_blocks` (plural)

Returns all valid JSON dicts in order. Caller takes the last one.

```python
def _extract_json_blocks(text: str) -> list[dict]:
    """Extract all valid JSON dicts from text, ordered by position."""
    blocks = []
    # Scan for all ```json ... ``` fences
    for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL):
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                blocks.append(obj)
        except (json.JSONDecodeError, ValueError):
            pass
    # Also scan for bare { ... } blocks
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text):
        try:
            obj = json.loads(m.group())
            if isinstance(obj, dict):
                blocks.append(obj)
        except (json.JSONDecodeError, ValueError):
            pass
    # Deduplicate by position (keep first occurrence of each)
    # Return in document order
    return blocks
```

The caller in the orchestrator:
```python
blocks = _extract_json_blocks(assistant_text)
if blocks:
    parsed = blocks[-1]  # last valid block wins
```

---

## Q5 — Session resume unit tests

Tests to add in `tests/test_dryrun.py`:

### Test: checkpoint from previous call is not extracted

```python
def test_session_resume_ignores_old_checkpoint():
    """Old checkpoint from a prior goose call must not be extracted
    when the current call produces empty output."""
    # Goose returns full session history including old checkpoint
    envelope = json.dumps({
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": '{"status":"blocked","summary":"old"}'}]},
            {"role": "user", "content": [{"type": "text", "text": "recovery instruction"}]},
        ]
    })
    # No new assistant message → empty_flag=True
    text, empty_flag = _extract_last_assistant_text(envelope)
    assert empty_flag is True
    assert text == ""
```

### Test: new checkpoint supersedes old one

```python
def test_session_resume_new_checkpoint_wins():
    """When goose returns history + new response, the new response's
    JSON block is extracted (last-wins)."""
    envelope = json.dumps({
        "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": '{"status":"blocked","summary":"old"}'}]},
            {"role": "user", "content": [{"type": "text", "text": "try again"}]},
            {"role": "assistant", "content": [{"type": "text", "text": '{"status":"done","summary":"new"}'}]},
        ]
    })
    text, empty_flag = _extract_last_assistant_text(envelope)
    assert empty_flag is False
    parsed = _extract_json_block(text)
    assert parsed["status"] == "done"
```

### Test: cascade extraction fallback

```python
def test_extract_json_blocks_cascade():
    """When last JSON block is malformed, fall back to previous."""
    text = '''
    {"status":"blocked","summary":"checkpoint"}
    Some work...
    {"status":"done","summary":"incomplete
    '''
    blocks = _extract_json_blocks(text)
    assert len(blocks) == 1
    assert blocks[0]["status"] == "blocked"
```

---

## Q8 — Metrics to add to iteration JSONL

### New fields on every IterationEntry

| Field | Type | When populated | Purpose |
|---|---|---|---|
| `checkpoint` | `bool` | Dev/QA response where `notes` contains "checkpoint" | Track checkpoint compliance |
| `json_blocks_found` | `int` | Every goose call | Count of valid JSON blocks extracted (cascade depth) |
| `json_blocks_cascade` | `bool` | When last block malformed but fallback succeeded | Track cascade effectiveness |
| `assistant_turns` | `int` | Every goose call | Number of assistant messages in envelope |
| `total_turns` | `int` | every goose call | Total messages in envelope (assistant + user) |

### New events to log

| Event | When | Fields |
|---|---|---|
| `dev.checkpoint` | Checkpoint detected in dev response | `task_label`, `iteration`, `checkpoint_summary` |
| `qa.checkpoint` | Checkpoint detected in QA response | `task_label`, `iteration`, `checkpoint_decision` |
| `json.cascade` | Cascade fallback used | `task_label`, `actor`, `blocks_found`, `block_used_index` |

### Derived metrics (computed from JSONL in analysis)

| Metric | Formula |
|---|---|
| Checkpoint compliance rate | `count(checkpoint=True) / total_dev_calls` |
| Malformed output rate (post-fix) | `count(status=error, raw_output has content) / total_calls` |
| Recovery invocations per task | `count(stage != normal) / unique tasks` |
| Cascade fallback rate | `count(json_blocks_cascade=True) / total_calls` |
| Avg assistant turns before JSON | `mean(assistant_turns where status != error)` |

---

## Additional discussion points

### A1: Should the checkpoint include the task label?

**Proposal:** Yes — the checkpoint should include the task label in the summary:

```json
{"status": "blocked", "summary": "Starting: P0.1.T3", "files_modified": [], "notes": "checkpoint"}
```

This makes iteration logs self-describing when grepping for a task's history.
The agent can read the task label from the recipe `{{ task_label }}` param.

No controversy expected — this is just a convention in the recipe instructions.

### A2: Should `schema.py` models validate enum values?

Today `status` is `str` — the orchestrator checks `status in ("done", "blocked")`
after extraction.  Should the Pydantic model enforce this with `Literal["done", "blocked"]`?

**Pros:** Agent gets immediate validation error if it writes `status: "complete"`.
**Cons:** The orchestrator's `_parse_dev_response` already validates.  Pydantic
validation in the agent is a nice-to-have, not a safety requirement (the
orchestrator is the gatekeeper per INV-1 through INV-5).

**My leaning:** Yes, add `Literal` types.  It's zero cost and gives the agent
better error messages.  But the orchestrator does NOT rely on Pydantic
validation for safety — it's defense-in-depth.

### A3: JSON-first is the only protocol

No backward compatibility flag.  The recipe always includes JSON-first
instructions and checkpoint requirements.  The old "JSON-last" approach
is removed from the recipe entirely.

This simplifies both the recipe YAML (no conditionals) and the orchestrator
(no version param to pass).  If we need to debug, we revert the recipe
file in git — not via a runtime flag.

### A4: What about the `--text` goose mode?

Today the tasker uses `--recipe` mode exclusively.  But goose also supports
`--text` mode (raw text input, no recipe).  If we ever switch to `--text`,
the JSON-first protocol must still work.

**My leaning:** Not a concern now.  `--text` mode doesn't support `--params`,
so it's incompatible with the tasker's current architecture.  If we ever
migrate, the JSON-first convention travels with the prompt text, not the
recipe template.

### A5: Should we track token usage?

Goose's JSON envelope doesn't include token counts.  But we can estimate
from the response text length.  Should we log `len(raw_stdout)` as a
proxy for output token usage?

**My leaning:** Yes — it's free data.  Add `output_chars: int` to
IterationEntry.  Not a precise token count but useful for trend analysis
(e.g., "are responses getting longer over iterations?").
