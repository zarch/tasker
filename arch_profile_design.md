# Architect Agent (ARCH) — Detailed Design Document

> **Status:** Draft
> **Date:** 2026-05-01
> **Authors:** Tasker project
> **Scope:** New ARCH agent + complementary fixes to reduce 85% error rate in production

---

## Table of Contents

1. [Problem Statement & Context](#1-problem-statement--context)
2. [Architecture Overview](#2-architecture-overview)
3. [Stuckness Detection](#3-stuckness-detection)
4. [ARCH Recipe & Prompt Design](#4-arch-recipe--prompt-design)
5. [Orchestrator Integration](#5-orchestrator-integration)
6. [Complementary Fixes](#6-complementary-fixes)
7. [Implementation Plan & Testing Strategy](#7-implementation-plan--testing-strategy)

---

# Architect Agent (ARCH) — Design Document

## 1. Problem Statement & Context

### The core problem

Tasker's DEV → QA feedback loop is fundamentally **non-resilient**. When a task gets stuck — the agent can't produce valid output, the LLM provider disconnects, or context overflows — the system responds by **repeating the same failing call pattern** hundreds of times rather than adapting. There is no mechanism to observe that a task is stuck and intervene with a new plan.

The result: **85% of all Goose agent calls across three production projects were wasted on error recovery with zero forward progress.**

---

### Production data (20,486 JSONL entries, 3 projects)

| Metric | Value |
|---|---|
| Total entries | 20,486 |
| Error entries | 17,452 (85%) |
| Successful entries | 3,034 (15%) |

**Error breakdown by root cause:**

| Root Cause | Count | % of Errors | Behavior |
|---|---|---|---|
| `malformed_output` (empty response) | ~13,900 | 79.7% | Goose rc=0, but no assistant message. 99% have `raw_output=""` — session context overflow from `subphase` scope. 1% have prose with no JSON. |
| `subprocess_failed` ("not connected") | ~3,100 | 17.8% | LLM provider disconnects. Concurrent tasker instances share provider → cascading failures. 5 retries × 11 recovery stages = **55 goose launches per event**. |
| `malformed_output` (prose, no JSON) | ~200 | 1.3% | Agent writes code/analysis but never outputs required JSON block. |
| `subprocess_failed` ("arg list too long") | ~204 | 1.2% | QA feedback text grows unbounded across iterations → exceeds OS `ARG_MAX`. |

**Task approval rates by project:**

| Project | Approved | Total | Rate | Wasted Entries |
|---|---|---|---|---|
| 11-gis-renderer | 17 | 48 | 35% | 88% |
| 13-cli | 4 | 37 | 11% | 100% |
| 16-python-bindings | 20 | 42 | 48% | 99% |

---

### The death spiral

When a task gets stuck, tasker enters a compounding failure pattern:

```
1 stuck task → ~25 feedback-loop iterations × 11 recovery stages = 275+ Goose calls
```

**Example:** P5-2.T3 generated **584 entries** (516 errors, 25 full iterations) with zero progress toward completion.

Three systemic factors make this spiral self-reinforcing:

- **Session context overflow** (`session_scope=subphase`): Sessions accumulate context across all tasks under a `###` heading. After several tasks, the context window fills → empty responses → recovery stages → more context → more failures.
- **Feedback parameter growth**: Each QA rejection appends full feedback text to the next DEV call. After 10+ iterations the param can be thousands of characters, eventually hitting OS `ARG_MAX`.
- **Recovery amplification**: Each recovery stage is itself a Goose call that can fail, spawning further recovery. The NORMAL → CONTINUE → SUBTASK → SUMMARIZE → RESTART pipeline (11 calls per cycle) assumes transient errors, not persistent stuckness.

---

### Why a simple circuit breaker isn't enough

A naïve solution would be: detect stuckness → skip the task → move on. This **doesn't work** because tasks have **sequential dependencies**:

```
1.1 Implement data model  →  1.2 Build API on model  →  1.3 Write tests for API
```

- If task 1.1 fails silently, tasks 1.2 and 1.3 have no foundation and will also fail.
- Skipping 1.1 means the entire subphase (and potentially all downstream subphases) is blocked.
- The task list is a dependency graph, not a flat list — you can't just skip a node.

A circuit breaker that marks a task as "failed" must also understand what to do about its dependents. That requires **re-planning**, not just skipping.

---

### Why existing pre-task decomposition isn't enough

Tasker already has a `--decompose` flag that invokes a decomposer agent **before** the task loop begins. This is useful but insufficient:

- **Pre-only, not mid-loop**: Decomposition happens once before implementation starts. If the plan is wrong or the task proves harder than expected mid-execution, there's no mechanism to re-decompose.
- **No recipe file exists yet**: The code in `orchestrator.py` `_decompose_task()` is present but the corresponding recipe/prompt file hasn't been written.
- **No stuckness detection**: Pre-task decomposition has no access to runtime signals (error rates, iteration count, feedback patterns) that indicate a task needs different treatment.
- **No re-plan of dependencies**: If decomposition reveals that task 1.1 should actually be 1.1a + 1.1b, there's no way to insert those subtasks into the running plan and update downstream dependencies.

---

### The need: a mid-loop Architect agent

What's missing is an agent that can **observe, diagnose, and re-plan during execution** — not just before it. The Architect agent (ARCH) is designed to:

1. **Detect stuckness** — monitor iteration count, error patterns, and feedback signals that indicate a task is not progressing.
2. **Diagnose the cause** — distinguish between transient failures (provider disconnect), structural problems (task too large, wrong approach), and systemic issues (context overflow).
3. **Intervene with a new plan** — decompose the remaining work, adjust the task graph, prune accumulated context, and restart the loop with a fresh strategy — all without breaking the dependency chain.

ARCH sits between the orchestrator and the DEV/QA loop as a **mid-loop recovery mechanism** — the role that neither pre-task decomposition nor simple retry logic can fill.

## 2. Architecture Overview

### 2.1 Agent Roles (updated)

| Agent | Role | Invoked When | Input | Output | Max Turns |
|-------|------|-------------|-------|--------|-----------|
| **DEV** | Implements code changes | Every task iteration | task_text, feedback, specs | `{"status":"done"\|"blocked", "summary":"...", "files_modified":[...]}` | 80-100 |
| **QA** | Reviews dev work against specs | After DEV returns "done"; triages blockers | task_text, dev_summary, diff, feedback | `{"decision":"approve"\|"reject"\|"needs_user_input", "feedback":"...", "concerns":[...]}` | 80-100 |
| **ARCH** | Diagnoses stuck tasks and re-plans | After N=3 consecutive recovery exhaustions | task_text, error_summary, code_state, spec_hints | `{"action":"redecompose"\|"clarify"\|"skip"\|"retry", ...}` | 20 |

### 2.2 Updated Pipeline Flow

```
Orchestrator.run()
  │
  ├─ Parse markdown task list → list[Phase]
  │
  ├─ FOR each pending task:
  │    │
  │    ├─ _process_task(task)
  │    │    │
  │    │    ├─ (optional) _decompose_task()     ← PRE-task decomposition
  │    │    │
  │    │    ├─ _run_feedback_loop(task)          ← DEV↔QA loop (up to max_iterations)
  │    │    │    │
  │    │    │    ├─ FOR iteration 1..max_iterations:
  │    │    │    │    │
  │    │    │    │    ├─ _run_dev_with_recovery()  → DevResponse
  │    │    │    │    │    ├─ Runs DEV through NORMAL→CONTINUE→SUBTASK→SUMMARIZE→RESTART
  │    │    │    │    │    └─ On exhaustion: increment consecutive_exhaustions counter
  │    │    │    │    │
  │    │    │    │    ├─ ★ STUCKNESS CHECK ★
  │    │    │    │    │    └─ IF consecutive_exhaustions >= 3:
  │    │    │    │    │         │
  │    │    │    │    │         ├─ _run_arch(task, error_summary, code_state) → ArchResponse
  │    │    │    │    │         │
  │    │    │    │    │         ├─ REDECOMPOSE:
  │    │    │    │    │         │    └─ Insert new subtasks into markdown
  │    │    │    │    │         │    └─ Re-parse → main loop picks up new tasks
  │    │    │    │    │         │    └─ BREAK feedback loop
  │    │    │    │    │         │
  │    │    │    │    │         ├─ CLARIFY:
  │    │    │    │    │         │    └─ Rewrite task text in markdown
  │    │    │    │    │         │    └─ Re-parse → RETRY with clarified text
  │    │    │    │    │         │    └─ Reset consecutive_exhaustions
  │    │    │    │    │         │
  │    │    │    │    │         ├─ SKIP:
  │    │    │    │    │         │    └─ Mark task done with "(skipped)" note
  │    │    │    │    │         │    └─ BREAK feedback loop
  │    │    │    │    │         │
  │    │    │    │    │         └─ RETRY:
  │    │    │    │    │              └─ Rotate sessions, reset counters
  │    │    │    │    │              └─ CONTINUE feedback loop
  │    │    │    │    │
  │    │    │    │    ├─ IF dev.status == "done":
  │    │    │    │    │    └─ _run_qa_with_recovery() → QAResponse
  │    │    │    │    │         ├─ approve → _finalize_task() → BREAK
  │    │    │    │    │         ├─ reject → build feedback → CONTINUE loop
  │    │    │    │    │         └─ needs_user_input → _interactive_chat_loop()
  │    │    │    │    │
  │    │    │    │    └─ IF dev.status == "blocked":
  │    │    │    │         └─ QA blocker triage → approve retry / reject
  │    │    │    │
  │    │    │    └─ On max_iterations: mark blocked, BREAK
  │    │    │
  │    │    └─ On success: _finalize_task() → mark [x], VCS commit
  │    │
  │    └─ NEXT task (or newly inserted subtasks from ARCH)
  │
  └─ Summary statistics
```

### 2.3 Integration Points

Three files need changes:

| File | Change | Detail |
|------|--------|--------|
| `src/tasker/orchestrator.py` | Stuckness detection + ARCH invocation | In `_run_feedback_loop()`, after DEV recovery exhaustion |
| `src/tasker/orchestrator.py` | ARCH decision application | New `_apply_arch_decision()` method |
| `src/tasker/models.py` | New data models | `ArchRequest`, `ArchResponse` dataclasses; `ARCH` in `Actor` enum |
| `src/tasker/parser.py` | Markdown mutation | New `insert_subtasks()` function |
| `src/tasker/main.py` | CLI flag | New `--arch` option (mirrors `--decompose`) |
| `recipes/recipe-arch.yaml` | New file | ARCH agent recipe |

### 2.4 Data Flow — JSON Schemas

**ArchRequest** (orchestrator → ARCH agent, passed as goose `--params`):

```json
{
  "task_label": "P5-2.T3",
  "task_text": "Implement LOD switch in GridPipeline::draw: if cell_screen_size > LOD_INSTANCED_THRESHOLD use instanced path; else use screen-pixel texture",
  "error_summary": "Task stuck for 3 consecutive recovery exhaustions (33 goose calls). All attempts returned empty assistant responses. No code changes detected.",
  "code_state_summary": "crates/hay-render/src/grid/pipeline.rs exists (342 lines). LOD_INSTANCED_THRESHOLD constant defined. No cell_screen_size() function yet.",
  "spec_hints": "See specs/arch/11-gis-renderer/05-grid-pipeline.md §3.2 LOD Switching"
}
```

**ArchResponse** (ARCH agent → orchestrator, extracted from agent output):

**Action: REDECOMPOSE**
```json
{
  "action": "redecompose",
  "reason": "Task requires 3 distinct pieces: a utility function, a constant, and a branching control flow — each should be a separate atomic task.",
  "subtasks": [
    {"label": "P5-2.T3a", "text": "Implement `cell_screen_size(meta: &GridMeta, camera: &Camera) -> f32` in grid/pipeline.rs"},
    {"label": "P5-2.T3b", "text": "Add LOD branch in `GridPipeline::draw`: dispatch to instanced or screen-pixel path based on `cell_screen_size()` vs `LOD_INSTANCED_THRESHOLD`"},
    {"label": "P5-2.T3c", "text": "Unit test: verify LOD branch selection at threshold boundary in tests/grid_pipeline_tests.rs"}
  ]
}
```

**Action: CLARIFY**
```json
{
  "action": "clarify",
  "reason": "Task text is ambiguous about where to add the branch. Clarifying the specific function and file.",
  "rewritten_task_text": "In `GridPipeline::draw()` (crates/hay-render/src/grid/pipeline.rs:89), add a conditional branch after the bind group setup: if `cell_screen_size(&self.meta, &camera) > LOD_INSTANCED_THRESHOLD { self.draw_instanced() } else { self.draw_screen_pixel() }`. Both methods already exist."
}
```

**Action: SKIP**
```json
{
  "action": "skip",
  "reason": "LOD switching was already implemented in P5-2.T2 via the unified shader path. This task is redundant."
}
```

**Action: RETRY**
```json
{
  "action": "retry",
  "reason": "Task appears solvable but the DEV session has accumulated stale context. A fresh session should resolve it.",
  "retry_hints": {"fresh_session": true, "max_turns": 50}
}
```


## 3. Stuckness Detection

### 3.1 Detection Criteria

A task is considered **stuck** when the DEV agent repeatedly fails to produce any valid output across multiple full-recovery cycles. We define two concrete, measurable criteria:

| Criterion | Metric | Threshold | Rationale |
|-----------|--------|-----------|-----------|
| **Consecutive recovery exhaustions** | Count of times the full recovery chain (NORMAL→CONTINUE→SUBTASK→SUMMARIZE→RESTART, ~11 calls) completes with zero valid responses | **≥ 3** | 3 full exhaustions = ~33 goose calls with no progress. This is clearly stuck, not a transient blip. |
| **Total iterations without success** | Count of feedback-loop iterations (DEV call + QA review) that produce no approved or "done" result | **≥ 10** | Safety net: even if recovery doesn't fully exhaust (e.g., some stages produce partial output), 10 failed iterations means the task is not converging. |

**Both criteria are checked** — either one triggers ARCH invocation.

### 3.2 State Tracking

The orchestrator tracks the following per-task state:

```python
# New fields in OrchestratorRunner (reset per task)
self._consecutive_exhaustions: int = 0       # resets on any successful DEV response
self._iterations_without_success: int = 0    # resets on QA "approve"
```

**Reset conditions:**

| Counter | When it resets |
|---------|---------------|
| `_consecutive_exhaustions` | Any DEV response that is NOT a synthetic blocked (i.e., the agent produced valid JSON) |
| `_iterations_without_success` | QA decision "approve" (task completes) or ARCH "retry" with fresh session |
| Both counters | When ARCH intervenes and applies a decision (re-plan resets the slate) |

**Tracking location:** Inside `_run_feedback_loop()`, after each DEV recovery cycle:

```python
# After _run_dev_with_recovery() returns:
if dev_response.status == "blocked" and dev_response._synthetic:
    # Recovery was fully exhausted — no valid output at any stage
    self._consecutive_exhaustions += 1
else:
    self._consecutive_exhaustions = 0  # agent produced something valid

self._iterations_without_success += 1
```

### 3.3 Detection Flow

```
_run_feedback_loop(task):
  │
  ├─ Initialize: consecutive_exhaustions = 0, iterations_without_success = 0
  │
  ├─ FOR iteration 1..max_iterations:
  │    │
  │    ├─ dev_response = _run_dev_with_recovery()
  │    │
  │    ├─ IF dev_response is synthetic blocked:
  │    │    └─ consecutive_exhaustions += 1
  │    ├─ ELSE:
  │    │    └─ consecutive_exhaustions = 0
  │    │
  │    ├─ iterations_without_success += 1
  │    │
  │    ├─ ★ STUCKNESS CHECK ★
  │    │    IF consecutive_exhaustions >= 3 OR iterations_without_success >= 10:
  │    │         │
  │    │         ├─ error_summary = _build_error_summary(task)
  │    │         ├─ code_state_summary = _build_code_state_summary()
  │    │         │
  │    │         ├─ arch_response = _run_arch(task, error_summary, code_state_summary)
  │    │         │
  │    │         ├─ IF arch_response is valid:
  │    │         │    ├─ _apply_arch_decision(task, phase, arch_response)
  │    │         │    ├─ Reset both counters
  │    │         │    │
  │    │         │    ├─ IF action == "redecompose" or "skip":
  │    │         │    │    └─ BREAK feedback loop (new tasks in queue)
  │    │         │    │
  │    │         │    └─ IF action == "clarify" or "retry":
  │    │         │         └─ CONTINUE feedback loop (with updated task/session)
  │    │         │
  │    │         └─ IF arch_response is invalid (ARCH failed):
  │    │              ├─ Log warning: "ARCH agent failed, falling back"
  │    │              ├─ Increment arch_failure counter
  │    │              └─ IF arch_failures >= 2:
  │    │                   └─ BREAK feedback loop (mark blocked, move on)
  │    │              └─ ELSE: CONTINUE loop (try ARCH again next exhaustion)
  │    │
  │    ├─ ... (normal QA review / blocker triage flow) ...
  │    │
  │    └─ On QA "approve": reset counters, finalize task
  │
  └─ On max_iterations: mark blocked
```

### 3.4 Edge Cases

| Edge Case | Handling |
|-----------|----------|
| **ARCH itself returns malformed output** | Retry ARCH once with a fresh session. If it fails again, fall back to current behavior (mark task blocked, continue to next task). Log clearly. |
| **ARCH says "retry" but task keeps getting stuck** | `_arch_invocation_count` tracks how many times ARCH has been called for this task. Cap at 2. After 2 ARCH invocations with no progress, hard-stop the task as blocked. |
| **Task has only 1 small subtask — should ARCH decompose?** | ARCH is an LLM — it decides. If the task is genuinely atomic, ARCH will choose "clarify" or "retry" instead of "redecompose". The prompt explicitly says: "Only decompose if the task contains ≥2 distinct pieces of work." |
| **ARCH says "skip" but downstream tasks exist** | ARCH's prompt requires it to verify that skipping is safe: "Only skip if the task is redundant or already completed by a previous task." The orchestrator trusts ARCH's judgment here — if ARCH is wrong, downstream DEV/QA will catch it. |
| **Feedback loop reaches max_iterations before stuckness threshold** | This is fine — max_iterations is a global safety limit. The stuckness threshold (3 exhaustions or 10 iterations) is designed to trigger well before max_iterations (default 30). |
| **Multiple ARCH invocations for the same task** | Cap at 2 per task. If the task is still stuck after 2 ARCH interventions, it's beyond automated recovery — mark blocked and let a human investigate. |
```


## 4. ARCH Recipe & Prompt Design

### 4.1 Recipe YAML — `recipes/recipe-arch.yaml`

```yaml
version: 1.0.0
title: Task Architect
description: >
  Diagnoses stuck tasks and re-plans them. Invoked when the DEV agent
  fails to produce valid output after multiple recovery attempts.
  Analyzes the task, specs, code state, and error history to produce
  a structured intervention plan.
author:
  contact: tasker

activities:
  - Analyze stuck tasks and diagnose failure causes
  - Decompose complex tasks into atomic subtasks
  - Clarify ambiguous task descriptions
  - Identify redundant or already-completed tasks

instructions: |
  You are a **Senior Software Architect** agent in an automated QA/Dev pipeline.
  Your role is to diagnose why a task is stuck and produce a recovery plan.

  ## Context

  You are invoked because the Developer agent has failed to complete a task
  after multiple attempts. The system has detected that the task is "stuck" —
  the developer could not produce valid output despite recovery efforts.

  ## What You Receive

  - **task_label**: The task identifier (e.g., "P5-2.T3")
  - **task_text**: The original task description
  - **error_summary**: Why the task got stuck (what went wrong)
  - **code_state_summary**: Current state of the codebase relevant to this task
  - **spec_hints**: References to spec/architecture documents

  ## Your Workflow

  1. **Read the task text carefully** — understand exactly what is being asked.
  2. **Read the error summary** — understand what went wrong.
  3. **Explore the codebase** — read the files mentioned in `code_state_summary`.
     Check what's already implemented and what's missing.
  4. **Read relevant specs** — if `spec_hints` references spec files, read them
     to understand the intended design.
  5. **Diagnose the root cause** — why is this task stuck? Common causes:
     - Task is too large (spans multiple functions/files/modules)
     - Task is ambiguous (unclear what to implement or where)
     - Task has hidden dependencies (needs types/code not yet implemented)
     - Task is redundant (work already done by a previous task)
  6. **Decide on an action** and produce the JSON response block.

  ## Decision Guide

  Choose **REDECOMPOSE** when:
  - The task contains ≥2 distinct pieces of work (e.g., "implement function X
    AND add tests for Y AND update module Z")
  - Each piece is independently testable and completable in 1-2 agent sessions
  - Subtasks should be ordered by dependency (foundations first)

  Choose **CLARIFY** when:
  - The task is atomic but the description is ambiguous
  - You can provide a more specific, step-by-step implementation guide
  - The task text doesn't specify which file, function, or module to modify

  Choose **SKIP** when:
  - The task's deliverable is already present in the codebase
  - The task was made obsolete by changes in a previous task
  - **WARNING**: Only skip if you are confident downstream tasks won't break

  Choose **RETRY** when:
  - The task is well-specified and atomic
  - The failure was likely due to session context issues (stale context, overflow)
  - A fresh session should be able to complete it

  ## Subtask Naming Convention

  When decomposing task P5-2.T3, name subtasks: P5-2.T3a, P5-2.T3b, P5-2.T3c, etc.
  Maximum 5 subtasks per decomposition. Each subtask text should be ≤300 chars.

  ## CRITICAL: Response Format

  You MUST respond with EXACTLY ONE JSON block enclosed in triple backticks:

  ````json
  {"action": "redecompose"|"clarify"|"skip"|"retry", "reason": "...", ...}
  ````

  Do NOT output anything else. No prose, no explanations outside the JSON block.

  ### Schema per action:

  **REDECOMPOSE:**
  ````json
  {"action": "redecompose", "reason": "...", "subtasks": [{"label": "P5-2.T3a", "text": "..."}, ...]}
  ````

  **CLARIFY:**
  ````json
  {"action": "clarify", "reason": "...", "rewritten_task_text": "..."}
  ````

  **SKIP:**
  ````json
  {"action": "skip", "reason": "..."}
  ````

  **RETRY:**
  ````json
  {"action": "retry", "reason": "...", "retry_hints": {"fresh_session": true}}
  ````

parameters:
  - key: task_label
    input_type: string
    requirement: optional
    default: ""
  - key: task_text
    input_type: string
    requirement: optional
    default: ""
  - key: error_summary
    input_type: string
    requirement: optional
    default: ""
  - key: code_state_summary
    input_type: string
    requirement: optional
    default: ""
  - key: spec_hints
    input_type: string
    requirement: optional
    default: ""

extensions:
  - type: builtin
    name: developer

prompt: |
  ## Stuck Task Analysis

  **Task:** {{ task_label }}: {{ task_text }}

  ## Why It's Stuck

  {{ error_summary }}

  ## Current Code State

  {{ code_state_summary }}

  ## Spec References

  {{ spec_hints }}

  ---

  Analyze the situation and output your JSON decision block now.
```

### 4.2 Prompt Engineering Notes

| Design Decision | Rationale |
|---|---|
| **error_summary is pre-computed** (not raw logs) | Raw JSONL logs would be thousands of tokens. The orchestrator summarizes: "3 consecutive exhaustions, 33 goose calls, all returned empty responses." This gives ARCH the signal without burning turns reading logs. |
| **code_state_summary is pre-computed** | ARCH shouldn't spend 5 turns exploring the file tree. The orchestrator runs `tree` and `head` on key files once, then passes a ~1000-char summary. ARCH can still read specific files if needed, but starts with context. |
| **Subtasks capped at 5** | Each subtask becomes a full DEV→QA cycle. More than 5 means the decomposition is too granular or the original task is truly massive (needs human intervention). |
| **Each subtask ≤300 chars** | Production data shows tasks with 70–200 chars work well. Longer task texts correlate with higher error rates (agent tries to do too much). |
| **Max turns = 20** | ARCH is analyzing, not implementing. It should read 3–5 files, think, and respond. 20 turns is generous — most analyses should complete in 5–8 turns. |
| **No feedback param** | ARCH doesn't see QA feedback history. It gets `error_summary` (what went wrong) and `code_state_summary` (where things stand). This prevents the unbounded growth problem that plagues DEV. |

### 4.3 Parameter Sizing

| Param | Typical Size | Max Size | Notes |
|-------|-------------|----------|-------|
| `task_label` | ~10 chars | ~20 chars | e.g., "P5-2.T3" |
| `task_text` | ~150 chars | ~500 chars | Original task text from markdown |
| `error_summary` | ~300 chars | ~800 chars | Pre-computed by orchestrator |
| `code_state_summary` | ~600 chars | ~1500 chars | File tree + key snippets |
| `spec_hints` | ~200 chars | ~500 chars | Spec file paths + section refs |
| **Total** | **~1260 chars** | **~3320 chars** | Well under OS ARG_MAX (~2MB) |

The ARCH params are designed to be compact — roughly 10× smaller than a typical DEV call with accumulated feedback. This ensures no "Argument list too long" risk and fast prompt rendering.


## 5. Orchestrator Integration

### 5.1 New State & Config

**OrchestratorRunner constructor — new parameters:**

```python
# In __init__():
arch_recipe: Path | None = None         # path to recipe-arch.yaml

# New instance state (reset per task):
self._consecutive_exhaustions: int = 0   # recovery exhaustions without valid output
self._iterations_without_success: int = 0  # feedback iterations without QA approve
self._arch_invocation_count: int = 0     # how many times ARCH called for this task
self._arch_session_name: str = ""        # ARCH goose session
```

**New constants (module-level in orchestrator.py):**

```python
_ARCH_EXHAUSTION_THRESHOLD = 3      # consecutive exhaustions before ARCH
_ARCH_ITERATION_THRESHOLD = 10      # total iterations without success before ARCH
_ARCH_MAX_INVOCATIONS = 2           # max ARCH calls per task
_ARCH_MAX_TURNS = 20                # goose --max-turns for ARCH
```

### 5.2 New Methods

**`_run_arch(task, error_summary, code_state_summary) -> ArchResponse | None`**

Invokes the ARCH goose agent. Builds an `ArchRequest`, rotates the ARCH session on repeated invocations, calls `_run_goose_with_ui()` with `max_turns=20`, and parses the response. Returns `None` on any failure (subprocess crash, malformed JSON).

**`_build_error_summary(task) -> str`**

Reads the JSONL log entries for this task, counts errors by type (malformed_output, connection failures, arg-too-long), and produces a ~500-char summary string like:

> "Task stuck after 87 entries (71 errors, 8 blocked, 4 feedback rounds). Error breakdown: 65 malformed_output, 6 connection failures, 0 arg-too-long. Consecutive recovery exhaustions: 3."

**`_build_code_state_summary() -> str`**

Runs `find` for `*.rs` files under the CWD (capped at 30 files), producing a compact file listing. If VCS is enabled, includes the last commit message. Returns ~800 chars.

**`_apply_arch_decision(task, phase, response) -> str`**

Dispatches on the ARCH response action:

| Action | What it does | Feedback loop |
|--------|-------------|---------------|
| `redecompose` | Insert subtasks into markdown, mark original `[x]` with "(decomposed)", set re-parse flag | **Break** — new tasks in queue |
| `clarify` | Rewrite task text in markdown, reset counters | **Continue** — retry with new text |
| `skip` | Mark task `[x]` with "(skipped: reason)", update markdown | **Break** — task done |
| `retry` | Rotate dev/qa sessions, reset counters | **Continue** — fresh start |

### 5.3 Modified Methods

**`_run_feedback_loop()` — add stuckness detection:**

After each `_run_dev_with_recovery()` call, track counters and check thresholds:

```python
# After dev_response = self._run_dev_with_recovery(...):

# Track stuckness
if _is_synthetic_blocked(dev_response):
    self._consecutive_exhaustions += 1
else:
    self._consecutive_exhaustions = 0
self._iterations_without_success += 1

# Stuckness check
if (
    self.arch_recipe
    and self._arch_invocation_count < _ARCH_MAX_INVOCATIONS
    and (
        self._consecutive_exhaustions >= _ARCH_EXHAUSTION_THRESHOLD
        or self._iterations_without_success >= _ARCH_ITERATION_THRESHOLD
    )
):
    arch_response = self._run_arch(task, ...)
    if arch_response:
        action = self._apply_arch_decision(task, phase, arch_response)
        if action in ("redecompose", "skip"):
            break  # exit feedback loop
    # else: fall through to normal blocked handling
```

**`run()` — re-parse after ARCH modifies markdown:**

After `_process_task()` returns, check if ARCH modified the markdown and re-parse:

```python
# In the main task loop, after _process_task():
if self._arch_modified_markdown:
    self.phases = parse_task_file(self.task_file)
    self._arch_modified_markdown = False
```

### 5.4 Markdown Mutation for REDECOMPOSE

**New function in `parser.py` — `insert_subtasks()`:**

Finds the stuck task's line in the markdown, appends "(decomposed)" to it, and inserts new `- [ ]` lines immediately after. Example:

**Before:**
```markdown
- [ ] P5-2.T3: Implement LOD switch in GridPipeline::draw
```

**After:**
```markdown
- [x] P5-2.T3: Implement LOD switch in GridPipeline::draw *(decomposed)*
- [ ] P5-2.T3a: Implement `cell_screen_size()` in grid/pipeline.rs
- [ ] P5-2.T3b: Add LOD branch in `GridPipeline::draw()`
- [ ] P5-2.T3c: Unit test for LOD branch selection
```

Key details:
- Matches the task line using the task label prefix (e.g., `P5-2.T3`)
- Preserves original indentation level
- Re-parse produces new `Task` objects for T3a, T3b, T3c with correct phase/subphase assignment
- The main loop's `find_next_task()` naturally picks them up in order

### 5.5 CLI Changes

**In `main.py` — new `--arch` flag:**

```python
arch: Path = typer.Option(
    None,
    "--arch",
    help="Path to the Architect agent recipe YAML. When set, ARCH is invoked on stuck tasks.",
    exists=True,
)
```

Usage:
```bash
uv run tasker --dev recipes/recipe-dev.yaml --qa recipes/recipe-qa.yaml \
    --arch recipes/recipe-arch.yaml specs/arch/11-gis-renderer/99-todo.md
```

### 5.6 ARCH Failure Handling

| Failure Mode | Handling |
|---|---|
| ARCH subprocess fails (rc≠0) | Retry once with fresh session. If still fails, fall back to current behavior. |
| ARCH returns malformed JSON | Same — retry once, then fall back. |
| ARCH returns unknown action | Log warning with raw output, treat as failure. |
| ARCH returns empty subtasks | Treat as "retry" — couldn't decompose, so just retry. |
| ARCH times out (20 turns) | Fall back to current behavior. |

**Fallback is always the status quo:** synthetic blocked → QA triage → feedback loop continues until `max_iterations`. ARCH is an improvement layer that never makes things worse.

## 6. Complementary Fixes

The ARCH agent addresses the #1 root cause (stuck tasks from ambiguous specifications). However, production data from 20,486 JSONL entries reveals three additional systemic failure modes that must be fixed independently. These fixes are prerequisite for the ARCH agent to be effective — without them, the ARCH agent itself will be drowned in retry noise.

### 6.1 Feedback Truncation

**Problem:** Each QA rejection in `_run_feedback_loop()` builds a `feedback` string by concatenating the full `qa_response.feedback` and all `concerns` with no size limit. After 10+ iterations, this param grows to thousands of characters. Combined with other params (`task_text`, `project_context`), it exceeds Linux `ARG_MAX` (~2MB), producing `"Argument list too long"` (1.2% of errors, ~204 entries). Even below the OS limit, bloated feedback pollutes the agent's context — it spends turns reading old feedback instead of working.

**Design:** Track feedback history as a bounded list. Keep only the last N rounds, truncate each round to M chars, and cap total at ~2000 chars. Replace the current string concatenation with a `_build_feedback_param()` helper.

**Constants (in `orchestrator.py`):**

```python
# -- Feedback truncation limits -------------------------------------------
_FEEDBACK_MAX_ROUNDS = 3      # keep only the last N QA rejection rounds
_FEEDBACK_MAX_CHARS_PER = 500  # truncate each round's feedback to M chars
_FEEDBACK_MAX_TOTAL = 2000    # hard cap on the total feedback param
```

**New helper (in `orchestrator.py`):**

```python
def _build_feedback_param(
    history: list[str],
    current_text: str,
    concerns: list[str],
) -> tuple[list[str], str]:
    """Append a feedback round to bounded history, return (new_history, feedback_param).

    *history* is a list of previously built feedback strings (one per rejection).
    The function appends the current round (truncated), trims to the last
    _FEEDBACK_MAX_ROUNDS entries, and builds a single string capped at
    _FEEDBACK_MAX_TOTAL chars.
    """
    # Build current round
    truncated_feedback = current_text[:_FEEDBACK_MAX_CHARS_PER]
    concerns_text = "\n".join(f"- {c}" for c in concerns)
    # Truncate combined round
    round_text = f"**Feedback:** {truncated_feedback}\n**Concerns:**\n{concerns_text}"
    round_text = round_text[:_FEEDBACK_MAX_CHARS_PER + 200]  # allow room for concerns

    history.append(round_text)
    # Keep only last N rounds
    if len(history) > _FEEDBACK_MAX_ROUNDS:
        history = history[-_FEEDBACK_MAX_ROUNDS:]

    # Build the full param
    parts: list[str] = []
    for i, h in enumerate(reversed(history), 1):
        label = "Latest" if i == 1 else f"Round {i}"
        parts.append(f"### {label}\n{h}")

    total = "\n\n".join(parts)
    if len(total) > _FEEDBACK_MAX_TOTAL:
        total = total[:_FEEDBACK_MAX_TOTAL] + "\n\n[... earlier feedback truncated]"
    return history, total
```

**Code change in `_run_feedback_loop()`:**

Add `feedback_history: list[str] = []` before the iteration loop, then replace the three places where `feedback` is assigned from a QA rejection:

```python
# BEFORE (current code, line ~1791):
feedback = (
    f"## QA Decision: REJECT\n\n"
    f"**Feedback:** {qa_response.feedback}\n\n"
    f"**Concerns:**\n"
)
for c in qa_response.concerns:
    feedback += f"- {c}\n"
feedback += "\nPlease fix ALL concerns above and re-submit."

# AFTER:
feedback_history, feedback = _build_feedback_param(
    feedback_history,
    qa_response.feedback,
    qa_response.concerns,
)
feedback += "\n\nPlease fix ALL concerns above and re-submit."
```

The same replacement applies to the blocker-triage rejection (line ~1769) and the chat-resolved feedback (line ~1664 — this one is small enough to not need truncation, but using the helper ensures consistency).

### 6.2 Connection-Error Hard Stop

**Problem:** When the LLM provider disconnects ("Error: not connected"), `run_goose_with_backoff()` retries up to 5 times with exponential backoff. The orchestrator's recovery stages then also retry (up to 11 attempts across NORMAL→CONTINUE→SUBTASK→SUMMARIZE→RESTART). Result: 5 × 11 = 55 goose launches per disconnection event. With 3 concurrent tasker instances, this cascades into thousands of failures (17.8% of errors, ~3,100 entries).

The key insight: a connection error is an **infrastructure problem**, not an **agent output quality problem**. Recovery stages are designed for the latter. Retrying through recovery stages for a provider outage wastes time, money, and log space.

**Design:** Track consecutive connection failures per task. After 3 consecutive connection failures, hard-stop the task immediately — bypass recovery entirely. Do not escalate through recovery stages for connection errors.

**New constant and tracking in `Orchestrator.__init__()`:**

```python
# -- Connection-error hard stop -------------------------------------------
_MAX_CONSECUTIVE_CONNECTION_ERRORS = 3  # hard-stop threshold per task
```

```python
# In Orchestrator.__init__(), add:
self._consecutive_connection_errors: int = 0
```

Reset at the start of each task (where sessions are rotated):

```python
# In _process_task() or wherever a new task begins, add:
self._consecutive_connection_errors = 0
```

**Code change in `_run_dev_with_recovery()`** — detect connection errors early and bypass recovery:

```python
# After the dev_result = self._run_goose_with_ui(...) call (line ~930),
# BEFORE the existing "if not dev_result.success:" block, add:

# ── Connection-error hard stop ──
if (
    not dev_result.success
    and not dev_result.timed_out
    and is_connection_error(dev_result.raw_stderr, dev_result.return_code)
):
    self._consecutive_connection_errors += 1
    log.warning(
        "dev.connection_error",
        task_label=task.label,
        consecutive=self._consecutive_connection_errors,
        max_allowed=self._MAX_CONSECUTIVE_CONNECTION_ERRORS,
    )
    if self._consecutive_connection_errors >= self._MAX_CONSECUTIVE_CONNECTION_ERRORS:
        log.error(
            "dev.connection_hard_stop",
            task_label=task.label,
            consecutive=self._consecutive_connection_errors,
        )
        self.ui.print_error(
            f"[{task.label}] Hard stop: {self._consecutive_connection_errors} consecutive "
            f"connection failures. The LLM provider appears to be down."
        )
        # Return blocked — the ARCH agent (or human) can decide to retry later
        return DevResponse(
            status="blocked",
            summary=f"LLM provider connection failed {self._consecutive_connection_errors} "
                    f"times consecutively.",
            files_modified=[],
            blocker_description=(
                f"The LLM provider has been unreachable for {self._consecutive_connection_errors} "
                f"consecutive attempts. This is likely an infrastructure issue (provider outage, "
                f"rate limit, or network problem), not a task complexity issue."
            ),
            blocker_suggestion=(
                "Wait for the provider to recover, then retry. "
                "Consider reducing concurrent tasker instances."
            ),
        )
    # Below threshold — don't escalate through recovery stages.
    # Just retry in current stage (connection errors are transient).
    self.ui.print_warning(
        f"[{task.label}] Connection error ({self._consecutive_connection_errors}/"
        f"{self._MAX_CONSECUTIVE_CONNECTION_ERRORS}). "
        f"Retrying in current stage ({stage.value})..."
    )
    continue  # retry in same stage, don't consume recovery budget
```

**Critical:** Reset the counter on any successful goose call:

```python
# At the top of the "if dev_response is not None:" block (successful parse):
self._consecutive_connection_errors = 0
```

**Also apply to `_run_qa_with_recovery()`** with the same pattern (track `self._qa_consecutive_connection_errors` or reuse the same counter). The QA side follows the same logic: connection errors don't count against recovery, but 3 in a row → synthetic reject.

**Reduce `rate_limit.max_retries` default from 5 to 3:**

In `models.py`, `RateLimitConfig.max_retries` default is currently `5`. With the hard-stop counter at the orchestrator level, we don't need as many backoff retries at the goose level. Reducing to 3 means worst case is 3 (backoff) × 3 (orchestrator retries) = 9 attempts before hard stop, instead of the current 5 × 11 = 55.

```python
# In models.py, RateLimitConfig:
max_retries: int = 3  # was 5
```

### 6.3 Session Scope Default Change

**Problem:** With `session_scope=subphase` (current default), goose sessions accumulate context across ALL tasks within a `###` group. After several tasks, the context window fills up. The agent then can't generate a complete response within `max_turns`, returning empty assistant messages → `malformed_output` (79.7% of errors).

**Evidence:** The latest production run used `--session-scope task` explicitly and produced significantly better results with far fewer empty/malformed responses.

**Design:** Change the default from `subphase` to `task`.

**Code change in `main.py`:**

```python
# BEFORE (line 103-106):
session_scope: str = typer.Option(
    "subphase",
    "--session-scope",
    help="When to rotate goose sessions: phase (per ## heading), subphase (per ### heading, default), or task (per task).",
)

# AFTER:
session_scope: str = typer.Option(
    "task",
    "--session-scope",
    help="When to rotate goose sessions: phase (per ## heading), subphase (per ### heading), or task (per task, default).",
)
```

Also update the default in `Orchestrator.__init__()`:

```python
# BEFORE:
session_scope: SessionScope = SessionScope.SUBPHASE,

# AFTER:
session_scope: SessionScope = SessionScope.TASK,
```

**Trade-off:** `task` scope means no cross-task context sharing. Agents won't "remember" what they did on the previous task. This is acceptable because:
1. The feedback param already carries relevant context from previous iterations within the same task.
2. Context overflow was the #1 cause of malformed output (79.7%).
3. Users who want shared context can explicitly set `--session-scope subphase`.

### 6.4 Concurrent Instance Guard

**Problem:** Three tasker instances running simultaneously hit the same LLM provider, causing rate limits and cascading "Error: not connected" failures. The backoff retries from one instance further stress the provider, creating a death spiral across all instances.

**Recommendation: Option C — document "don't run concurrent instances against the same provider."**

Rationale for rejecting Options A and B:
- **Option A (advisory lock file):** Adds complexity (stale lock cleanup, crash recovery, NFS edge cases). The lock file approach also breaks legitimate use cases (running tasker against different providers simultaneously).
- **Option B (staggered start with random delay):** Doesn't actually solve the problem — instances will still overlap during long runs. It just delays the collision.
- **Option C (documentation):** The real fix is the connection-error hard stop (§6.2). Combined with reduced backoff retries, a single tasker instance will fail fast and recover. Multiple instances against different providers (or different models) are fine. The only problematic case is N instances against the same provider — and that's a usage constraint, not a software bug.

**Documentation addition** (in `README.md`):

```markdown
### Running Multiple Instances

> **⚠️ Warning:** Do not run multiple `tasker` instances simultaneously against the
> same LLM provider and model. Each tasker instance launches goose subprocesses
> continuously, and concurrent instances will exhaust the provider's rate limit,
> causing cascading connection failures.
>
> If you need to parallelize work:
> - Use different providers (e.g., one with `--provider openai`, another with `--provider anthropic`)
> - Use different models on the same provider
> - Run instances sequentially, not concurrently
```

### 6.5 Priority & Impact Table

| Fix | Effort | Est. Error Reduction | Dependencies | Priority |
|---|---|---|---|---|
| **§6.3 Session scope default → `task`** | Small (2-line change) | ~60-70% (eliminates most context-overflow malformed_output) | None | **P0 — ship immediately** |
| **§6.2 Connection-error hard stop** | Medium (modify 2 methods, add counter) | ~15-18% (eliminates death spiral) | None | **P0 — ship with ARCH** |
| **§6.1 Feedback truncation** | Small (add helper, replace 3 call sites) | ~1-2% (eliminates ARG_MAX), prevents context pollution for ARCH | None | **P1 — ship with ARCH** |
| **§6.4 Concurrent instance docs** | Trivial (README update) | Preventive (not measured in current data) | §6.2 makes it less critical | **P2 — anytime** |

**Why this ordering:**

1. **§6.3** is a one-line default change that eliminates the majority of malformed_output errors (empty responses from context overflow). It requires no new code — just changing a default. Ship it today.

2. **§6.2** is the second highest impact. The connection-error hard stop prevents 55-attempt death spirals and is a prerequisite for the ARCH agent to work well — without it, the ARCH agent's goose calls will also get caught in cascading retries when the provider is down.

3. **§6.1** is lower measured impact (only 1.2% of errors are ARG_MAX) but is critical for the ARCH agent's feedback loop to work. Without truncation, the ARCH agent's own feedback param will also grow unbounded in long decomposition/review cycles.

4. **§6.4** is a documentation-only change that prevents a known failure mode for multi-user deployments.

**Combined impact:** §6.3 + §6.2 together address ~80% of current production errors. §6.1 prevents a latent failure mode that will become critical once the ARCH agent enables longer feedback loops (decomposition → subtask review → re-integration).


## 7. Implementation Plan & Testing Strategy

### 7.1 Implementation Phases

#### Phase 1 — Foundation (models, recipe, CLI flag)

No orchestrator changes. Can be tested independently with unit tests.

| File | Change |
|------|--------|
| `src/tasker/models.py` | Add `ARCH` to `Actor` enum. Add `ArchRequest` dataclass with `to_params()`. Add `ArchResponse` dataclass with `action`, `reason`, optional `subtasks`, `rewritten_task_text`, `retry_hints`. Add `_parse_arch_response()` helper. |
| `recipes/recipe-arch.yaml` | New file — full ARCH agent recipe (see §4.1). |
| `src/tasker/main.py` | Add `--arch` CLI option (optional Path, like `--decompose`). |
| `src/tasker/ui.py` | Add `📐` icon for ARCH actor in iteration table display. |

#### Phase 2 — Core Integration (orchestrator + parser)

The main feature. Depends on Phase 1.

| File | Change |
|------|--------|
| `src/tasker/orchestrator.py` | Add stuckness constants and state fields. Add `_run_arch()`, `_build_error_summary()`, `_build_code_state_summary()`, `_apply_arch_decision()`. Modify `_run_feedback_loop()` to track counters and invoke ARCH. Modify `run()` to re-parse after ARCH modifies markdown. |
| `src/tasker/parser.py` | Add `insert_subtasks(path, task, subtasks)` function. Add `rewrite_task_text(path, task, new_text)` function. |
| `src/tasker/models.py` | Add `is_arch_modified` flag support if needed. |

#### Phase 3 — Complementary Fixes

Independent of ARCH. Can be shipped before or after Phase 2.

| File | Change |
|------|--------|
| `src/tasker/orchestrator.py` | Add `_build_feedback_param()` helper. Replace 3 feedback-building call sites. Add connection-error hard stop in `_run_dev_with_recovery()` and `_run_qa_with_recovery()`. |
| `src/tasker/models.py` | Change `RateLimitConfig.max_retries` default from 5 to 3. |
| `src/tasker/main.py` | Change `--session-scope` default from `"subphase"` to `"task"`. |
| `src/tasker/orchestrator.py` | Change `session_scope` param default from `SessionScope.SUBPHASE` to `SessionScope.TASK`. |

### 7.2 Test Plan

All tests are dry-run (no real goose subprocess), following the existing pattern in `tests/test_dryrun.py`.

#### Model Tests

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_arch_request_to_params` | `ArchRequest.to_params()` returns all 5 declared recipe params | None | Dict has keys: task_label, task_text, error_summary, code_state_summary, spec_hints. All values are strings. |
| `test_arch_response_parse_redecompose` | Parse valid REDECOMPOSE JSON | None | `action=="redecompose"`, `len(subtasks)==3`, subtask labels match convention. |
| `test_arch_response_parse_clarify` | Parse valid CLARIFY JSON | None | `action=="clarify"`, `rewritten_task_text` is non-empty. |
| `test_arch_response_parse_skip` | Parse valid SKIP JSON | None | `action=="skip"`, `reason` is non-empty. |
| `test_arch_response_parse_retry` | Parse valid RETRY JSON | None | `action=="retry"`, `retry_hints.fresh_session==True`. |
| `test_arch_response_rejects_unknown_action` | Malformed JSON with action="explode" | None | Returns None (parse failure). |
| `test_arch_response_empty_subtasks` | REDECOMPOSE with empty subtasks list | None | Treated as "retry" fallback. |
| `test_actor_enum_has_arch` | `Actor.ARCH` exists | None | `Actor.ARCH.value == "arch"`. |

#### Stuckness Detection Tests

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_counter_increment_on_synthetic_blocked` | Synthetic blocked response increments counter | `run_goose` | After 1 synthetic blocked: `consecutive_exhaustions == 1`. |
| `test_counter_reset_on_valid_response` | Valid DEV response resets counter | `run_goose` | After synthetic then valid: `consecutive_exhaustions == 0`. |
| `test_arch_triggered_at_threshold` | 3 consecutive exhaustions triggers ARCH | `run_goose` | ARCH invocation count == 1 after 3 exhaustions. |
| `test_arch_not_triggered_below_threshold` | 2 exhaustions does NOT trigger ARCH | `run_goose` | ARCH not invoked. |
| `test_arch_iteration_threshold` | 10 iterations without success triggers ARCH | `run_goose` | ARCH invoked even with no consecutive exhaustions. |
| `test_arch_max_invocations` | ARCH called max 2 times per task | `run_goose` | After 2 ARCH calls, no more invocations even if still stuck. |

#### ARCH Invocation Tests

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_run_arch_success` | ARCH returns valid ArchResponse | `run_goose` returns JSON | Response action matches expected. |
| `test_run_arch_malformed` | ARCH returns garbage | `run_goose` returns prose | Returns None, logged as warning. |
| `test_run_arch_subprocess_fail` | ARCH goose process crashes | `run_goose` returns rc=1 | Returns None, logged as warning. |
| `test_run_arch_session_rotation` | Second ARCH call rotates session | `run_goose` | Session name changes on 2nd call. |

#### ARCH Decision Application Tests

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_apply_redecompose` | Subtasks inserted into markdown | File I/O | Markdown file has 3 new `- [ ]` lines after original task. Original marked `[x]` with "(decomposed)". |
| `test_apply_clarify` | Task text rewritten | File I/O | Markdown has new task text. In-memory task.text updated. Counters reset. |
| `test_apply_skip` | Task marked done with skip note | File I/O | Task.done == True. Markdown has "(skipped: reason)". |
| `test_apply_retry` | Sessions rotated, counters reset | None | dev/qa session names changed. Exhaustion counter == 0. |

#### Markdown Mutation Tests

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_insert_subtasks_basic` | Insert 3 subtasks after a task | File I/O with tmp file | Lines inserted at correct position. Original line has "(decomposed)". |
| `test_insert_subtasks_preserves_indent` | Subtasks match original task's indentation | File I/O | New lines have same leading whitespace. |
| `test_insert_subtasks_roundtrip` | Insert then re-parse | File I/O | `parse_task_file()` returns correct number of new tasks with correct labels/texts. |
| `test_rewrite_task_text` | Change task text in-place | File I/O | Only the target line changes, rest of file unchanged. |
| `test_insert_subtasks_not_found` | Task label not in file | File I/O | Raises ValueError. |

#### Feedback Truncation Tests

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_feedback_truncation_keeps_last_3` | History trimmed to last 3 rounds | None | After 5 rounds, `len(history) == 3`. |
| `test_feedback_truncation_per_round_cap` | Each round capped at 500 chars | None | No single round exceeds 500 chars. |
| `test_feedback_truncation_total_cap` | Total feedback capped at 2000 chars | None | Final string length ≤ 2050 (2000 + truncation notice). |
| `test_feedback_truncation_concerns` | Concerns list included | None | Concerns appear in output. |

#### Integration Test

| Test | What it tests | Mocked | Assertions |
|------|--------------|--------|------------|
| `test_stuck_task_triggers_arch_redecompose` | Full pipeline: task gets stuck → ARCH decomposes → new tasks run | `run_goose` (return synthetic blocked 3×, then ARCH returns redecompose, then DEV succeeds on subtasks) | Original task marked decomposed. 2 new tasks approved. Pipeline continues. |

### 7.3 Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ARCH itself gets stuck (malformed output) | Medium (same LLM reliability issues) | Low (fallback exists) | Limited to 20 turns. Retry once. Fall back to current behavior. |
| REDECOMPOSE creates too many tiny subtasks | Low (prompt limits to 5) | Low | Cap at 5 subtasks. Each must be ≤300 chars. |
| Markdown insertion breaks parsing | Low (simple line insertion) | Medium (pipeline stops) | Round-trip test (insert → re-parse). Fallback: if re-parse fails, log error and skip. |
| ARCH adds latency to slow pipeline | Medium (extra goose call) | Low (only on stuck tasks, 20 turns max) | Only invoked when stuck (~33 calls already wasted). ARCH at 20 turns is faster than 1 full recovery cycle. |
| ARCH "skip" decision breaks dependencies | Low (prompt warns ARCH) | High (downstream tasks fail) | Prompt requires ARCH to verify skip is safe. If downstream tasks fail, they'll trigger ARCH again for further diagnosis. |
| Feedback truncation loses critical context | Low (keeps last 3 rounds) | Medium | The most recent feedback is the most relevant. Old feedback was already addressed (or not). |

### 7.4 Acceptance Criteria

The feature is "done" when all of the following are true:

1. **ARCH is invoked** when a task hits 3 consecutive recovery exhaustions or 10 iterations without success.
2. **REDECOMPOSE** correctly inserts subtasks into the markdown file; the pipeline re-parses and runs new tasks sequentially.
3. **CLARIFY** rewrites the task text in the markdown; the DEV agent retries with the updated text.
4. **SKIP** marks the task as done with a reason; the pipeline continues to the next task.
5. **RETRY** rotates sessions and resets counters; the feedback loop continues.
6. **ARCH failure** is handled gracefully — falls back to current behavior (synthetic blocked, QA triage).
7. **Feedback param** is truncated to last 3 rounds, max 2000 chars total.
8. **Connection errors** cause hard stop after 3 consecutive failures per task.
9. **Session scope default** is `task` (not `subphase`).
10. **All 47 existing tests** still pass unchanged.
11. **New tests** cover: ARCH models, stuckness detection, ARCH invocation, decision application, markdown mutation, feedback truncation.
12. **No regression** in approved-task performance (tasks that currently succeed should still succeed).

### 7.5 Rollout Strategy

1. **Ship Phase 3 first** (complementary fixes) — these are small, independent changes with high impact. The session scope default alone eliminates ~60-70% of errors.
2. **Ship Phase 1** (foundation) — models, recipe, CLI flag. No behavior change unless `--arch` is specified.
3. **Ship Phase 2** (core integration) — the full ARCH feature. Test with a single project first (`11-gis-renderer`) before enabling across all projects.
4. **Monitor** the JSONL logs for ARCH-specific entries. Verify:
   - ARCH invocation rate (should be <10% of tasks)
   - ARCH action distribution (expect mostly "redecompose" and "clarify")
   - Post-ARCH task success rate (should be >50% for redecomposed tasks)
   - ARCH failure rate (should be <10%)
