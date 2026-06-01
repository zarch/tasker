# Structured Output Improvement — Spec Series

## Problem

The tasker dev-QA loop suffers from **malformed output** errors where the
goose agent fails to produce the required JSON block.  Analysis of 5,869
iteration records from a real run shows three distinct failure modes:

| Category | Count | % | Root cause |
|---|---|---|---|
| Empty output | 3,612 | 85.5% | Goose returns rc=0, valid envelope, zero assistant messages (structural) |
| Content, no JSON | 587 | 13.9% | Agent did real work but ran out of output tokens before JSON block |
| Timeout | 28 | 0.7% | Goose killed after 600s |

**Already fixed** (PR #1): circuit breaker + `[~]` marker + empty-output detection
eliminates the 85.5% waste path.  Each stuck task now burns ≤3 goose calls
instead of ~190.

**This spec series** addresses the remaining 13.9% — the agent works but
fails to produce valid structured output.

---

## Spec files

| File | Topic | Status |
|---|---|---|
| `01-current-flow.md` | How the dev-QA loop works today | ✅ reviewed |
| `02-pydantic-models.md` | Export Pydantic response models for the agent | ✅ decided |
| `03-json-first-protocol.md` | JSON-first output protocol (checkpoint + finalize) | ✅ decided |
| `04-recipe-changes.md` | Updated recipe instructions for both agents | ✅ decided |
| `05-control-safety.md` | How the orchestrator maintains control and safety guarantees | ✅ decided |
| `06-open-questions.md` | Decisions log + additional discussion points | ✅ 8/8 decided, 5 additional |
| `07-vcs-auto-init.md` | Auto-init git in subdirectories for multi-repo workspaces | ✅ decided |

---

---

## Task breakdown

All 36 implementation tasks are in `99-todo.tasks.jsonl`, organized in 9 phases:

| Phase | Title | Tasks | Deliverable |
|---|---|---|---|
| 1 | Pydantic Schema & Dependency | 3 | `schema.py` + pydantic dep + tests |
| 2 | Cascade JSON Extraction | 3 | `_extract_json_blocks` + cascade fields + tests |
| 3 | PYTHONPATH Injection | 2 | env var setup so agents can `from tasker.schema import ...` |
| 4 | Orchestrator Pydantic Validation | 5 | Pydantic-first `_parse_*` with ad-hoc fallback + tests |
| 5 | Metrics & Logging | 7 | IterationEntry fields, stale-only detection, checkpoint + cascade logging |
| 6 | Recipe JSON-First Rewrite | 4 | All 4 recipes updated with mandatory checkpoint + Pydantic import |
| 7 | Session Resume Safety Tests | 3 | Old-checkpoint-ignored, new-wins, cascade-from-history |
| 8 | Cleanup & Dead Code Removal | 2 | Remove old `_extract_json_block`, final verification |
| 9 | VCS Auto-Init | 7 | Dir extraction from task text, `init_subdir`, orchestrator scan |

---

## Guiding principles

1. **The orchestrator stays in control.** Agents are stateless workers.
   The orchestrator decides what happens next — always.
2. **Graceful degradation.** If the agent can't produce a Pydantic object,
   the system falls back to the existing `_extract_json_block` + recovery
   stages.  New code wraps old code; it doesn't replace it.
3. **Zero regression.** The 87 existing tests must pass unchanged.
   New behavior is additive.
4. **Observable.** Every new decision point produces a log entry and an
   `IterationEntry` so the iteration JSONL tells the full story.
