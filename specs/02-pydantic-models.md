# 02 — Pydantic Response Models

## Goal

Give the agent a Python importable schema it can use to produce
structurally correct JSON output, instead of copy-pasting from
a recipe example.

---

## 2.1 What we export

Create a new module `src/tasker/schema.py` containing Pydantic v2 models
that mirror the existing `DevResponse` and `QAResponse` dataclasses:

```python
# src/tasker/schema.py
"""Pydantic models for structured agent output.

Importable by goose agents via PYTHONPATH injection:

    from tasker.schema import DevResponse, QAResponse
    print(DevResponse(status="done", summary="...", files_modified=[]).model_dump_json())
"""
from pydantic import BaseModel, Field


class DevResponse(BaseModel):
    """Dev agent structured response."""

    status: str = Field(description='"done" or "blocked"')
    summary: str = Field(description="What was implemented or attempted")
    files_modified: list[str] = Field(default_factory=list)
    notes: str = ""
    blocker_description: str = ""
    blocker_suggestion: str = ""


class QAResponse(BaseModel):
    """QA agent structured response."""

    decision: str = Field(description='"approve", "reject", or "needs_user_input"')
    feedback: str = Field(description="Explanation of the decision")
    concerns: list[str] = Field(default_factory=list)
    user_question: str = ""
```

### Why Pydantic, not dataclasses?

- `model_dump_json()` produces clean JSON — the agent can `print()` it directly
- Field descriptions appear in `model_json_schema()` — self-documenting
- The agent can call `DevResponse.model_validate_json(text)` to check its own output
- Pydantic is already a transitive dependency (via `pydantic-settings` in the tasker)

### Why a separate module, not reuse `models.py`?

- `models.py` has dataclasses with `to_params()`, `to_dict()`, and orch-specific logic
- The agent needs zero-dependency Pydantic models — no tasker internals leaked
- `schema.py` is the public API surface exposed to agents

---

## 2.2 How the agent gets access

The tasker sets `PYTHONPATH` before launching goose:

```python
# In goose.py, build_goose_command() or run_goose():
import tasker
tasker_src = str(Path(tasker.__file__).parent.parent)  # points to src/
env["PYTHONPATH"] = tasker_src  # or prepend to existing
```

Then the recipe instructions say:

```
## Structured Output (recommended)

A Pydantic model is available for guaranteed-valid JSON output:

    from tasker.schema import DevResponse
    resp = DevResponse(status="done", summary="What you did", files_modified=["path/to/file"])
    print(resp.model_dump_json())

This always produces valid JSON matching the expected schema.
```

### Fallback: agent can't import

If the agent runs in an environment where Python import isn't available
(e.g., it only uses shell commands), it falls back to the current approach
of writing raw JSON in a markdown code block.  The orchestrator's
`_extract_json_block` handles both paths.

---

## 2.3 Orchestrator-side validation (double-check)

After `_extract_json_block` returns a dict, the orchestrator currently
calls `_parse_dev_response(raw, parsed)` which does ad-hoc dict field
extraction.  We can strengthen this:

```python
# In orchestrator.py
from tasker.schema import DevResponse as DevResponseSchema, QAResponse as QAResponseSchema

def _parse_dev_response(raw: str, parsed: dict | None) -> DevResponse | None:
    if parsed is None:
        return None
    try:
        validated = DevResponseSchema.model_validate(parsed)
        return DevResponse(
            status=validated.status,
            summary=validated.summary,
            files_modified=validated.files_modified,
            notes=validated.notes,
            blocker_description=validated.blocker_description,
            blocker_suggestion=validated.blocker_suggestion,
        )
    except ValidationError:
        # Fall back to ad-hoc parsing for backward compatibility
        ...
```

This catches malformed-but-JSON cases like `{"status": "DON"}` (typo)
that the current parser would accept.

---

## 2.4 What this does NOT fix

The Pydantic model guarantees **structural correctness** when the agent
actually calls it.  But it does NOT fix:

- **Agent runs out of tokens** before reaching the `print(resp.model_dump_json())` line
- **Agent forgets** to call the import at all (14% of errors — the agent
  does real work, writes prose, then the response is truncated)
- **Empty output** (already fixed by circuit breaker)

The Pydantic model is a quality-of-life improvement for the 587 cases
where the agent *almost* produced valid output.  The JSON-first protocol
(spec 03) addresses the deeper problem.
