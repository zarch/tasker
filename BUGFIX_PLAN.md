# Tasker Stuckness Detection — Bug Fix Plan

**Created**: 2026-05-02 02:21
**File being edited**: `/src/gis/tools/tasker/src/tasker/orchestrator.py`
**Test file**: `/src/gis/tools/tasker/tests/test_dryrun.py`
**Run tests**: `cd /src/gis/tools/tasker && uv run python tests/test_dryrun.py`

---

## Context: Why These Fixes Are Needed

The `tasker` tool runs a QA↔Dev feedback loop with an ARCH (architect) agent that's supposed
to intervene when tasks get stuck. Analysis of a real run (406 iterations, 60 tasks) revealed:

1. **ARCH agent was NEVER invoked** despite `arch=enabled` — 0 arch entries in JSONL
2. **57% malformed_output rate** — goose reads too many files, consuming max_turns before producing JSON
3. **"Dishonest recovery"** — dev agent fabricates "already done" when no code was written
4. **P7-2.T5 marked blocked despite QA approval** — control flow bug

### Root Causes Identified

- **Bug 1 (done)**: `_consecutive_exhaustions` only incremented for synthetic blocked (summary contains
  "multiple recovery attempts"), not for real blocked events. Changed to count ALL blocked.
- **Bug 2**: `_is_task_stuck()` runs at the TOP of the loop iteration, BEFORE dev+QA run. Counters
  from the current iteration aren't visible yet. Need to evaluate at the BOTTOM of the loop
  and defer ARCH invocation to the TOP of the next iteration.
- **Bug 3**: Threshold of 3 is too high given that real blocked events now count (Bug 1 fix means
  more increments). Lower to 2.
- **Bug 4**: Counters are instance variables reset on every process restart. The tasker was restarted
  9 times in the analyzed run. Need to restore counters from JSONL on task start.
- **Bug 5**: In blocker triage, QA can approve but the code path doesn't return immediately,
  allowing a second QA invocation that produces a blocked entry.
- **Bug 6**: Recovery prompts don't warn against fabrication. Dev claims "already completed" with
  no actual code changes, wasting QA cycles.
- **Bug 7**: No validation that dev's "done" claim matches actual file changes.

---

## Current State of Edits

### ✅ Bug Fix 1: DONE
**Lines changed**: ~2070-2083 in orchestrator.py

Before (approx line 2072):
```python
if "multiple recovery attempts" in (dev_response.summary or ""):
    self._consecutive_exhaustions += 1
```

After:
```python
if dev_response.status == "blocked":
    # Track stuckness: ANY blocked event counts toward exhaustion.
    self._consecutive_exhaustions += 1
    log.info(
        "dev.stuckness_counter_incremented",
        task_label=task.label,
        consecutive_exhaustions=self._consecutive_exhaustions,
        iterations_without_approval=self._iterations_without_approval,
    )
```

Also added instance variable `_pending_arch_check: bool = False` at line ~319.

---

## Remaining Fixes — Detailed Instructions

### Bug Fix 2: Move stuckness check after dev+QA cycle

**Problem**: `_is_task_stuck()` at top of loop sees counters from *previous* iteration only.
After Bug Fix 1, `_consecutive_exhaustions` increments inside the loop (dev blocked branch ~line 2075),
but `_is_task_stuck()` already ran at line ~2022 before dev was called.

**Solution**:
1. At the **bottom** of the for-loop body (after QA reject branch, after `_iterations_without_approval += 1`),
   add a check: if `_is_task_stuck(task)` is true, set `self._pending_arch_check = True`.
2. At the **top** of the for-loop, replace the current `_is_task_stuck()` check with:
   `if self._pending_arch_check and self.arch_recipe:` — then invoke ARCH (same code as now).
3. Reset `self._pending_arch_check = False` after ARCH invocation.

**Exact locations in orchestrator.py**:

Top of loop (lines ~2020-2058): Replace the block starting with
`if self._is_task_stuck(task) and self.arch_recipe:` with:
```python
            # ── Deferred stuckness check: invoke ARCH if flagged last iteration ──
            if self._pending_arch_check and self.arch_recipe:
                self._pending_arch_check = False
                # ... (rest of ARCH invocation code stays the same)
```

Bottom of loop — after QA reject branch (after `feedback = self._truncate_feedback(feedback)`
around line ~2295, just before the loop closes):
```python
                # ── Evaluate stuckness after counters updated ──
                if self._is_task_stuck(task):
                    self._pending_arch_check = True
```

Also need to handle the case where dev blocked + QA blocked in the SAME iteration — the
counter increment happens in the dev blocked branch, then QA runs, and at the bottom the
`_is_task_stuck()` check will see the updated counter.

**Important**: Also reset `_pending_arch_check = False` when task changes (around line 464-466
where `_consecutive_exhaustions` and `_iterations_without_approval` are reset).

---

### Bug Fix 3: Lower exhaustion threshold 3 → 2

**Location**: Line 198 in orchestrator.py

Change:
```python
_STUCK_EXHAUSTION_THRESHOLD = 3
```
To:
```python
_STUCK_EXHAUSTION_THRESHOLD = 2
```

**Why**: With Bug Fix 1 counting ALL blocked events (not just synthetic), the counter increments
more frequently. A threshold of 2 means ARCH triggers after 2 consecutive blocked cycles,
which is appropriate — by that point the task is clearly stuck.

---

### Bug Fix 4: Persist stuckness counters across process restarts

**Problem**: Instance variables reset to 0 on every `__init__()`. Tasker was restarted 9 times
in the analyzed run.

**Solution**: Add a method `_restore_stuckness_from_log(task)` that scans the JSONL iteration
log for entries matching the current task label, then computes counters from history.

**New method** (add after `_is_task_stuck` around line 1223):
```python
    def _restore_stuckness_from_log(self, task: Task) -> None:
        """Restore stuckness counters from JSONL log for a resumed task.

        Scans iteration history for the given task and replays counter state
        so that stuckness detection works across process restarts.
        """
        entries = self.log.read()
        task_entries = [e for e in entries if e.get("task_label") == task.label]

        if not task_entries:
            return

        exhaustions = 0
        iters_without_approval = 0

        for entry in task_entries:
            status = entry.get("status", "")
            actor = entry.get("actor", "")

            if status == "blocked" and actor == "dev":
                exhaustions += 1
            elif status == "approved":
                # QA approval resets everything
                exhaustions = 0
                iters_without_approval = 0
            elif status == "rejected" and actor == "qa":
                iters_without_approval += 1
            elif status in ("done",) and actor == "dev":
                # Dev done doesn't reset — QA still needs to approve
                pass

        self._consecutive_exhaustions = exhaustions
        self._iterations_without_approval = iters_without_approval
        log.info(
            "stuckness.restored_from_log",
            task_label=task.label,
            consecutive_exhaustions=exhaustions,
            iterations_without_approval=iters_without_approval,
            total_entries=len(task_entries),
        )
```

**Call site**: In the task-change detection block around line 460-466, add after the counter reset:
```python
            # Reset stuckness counters for new task
            if self._stuck_task_label != task.label:
                self._stuck_task_label = task.label
                self._consecutive_exhaustions = 0
                self._iterations_without_approval = 0
                self._pending_arch_check = False
                # Restore counters from previous runs if available
                self._restore_stuckness_from_log(task)
```

**Note**: The method reads from `self.log` which is an `IterationLog` instance. Check `log.py`
to confirm the `read()` method returns list of dicts. The `IterationLog` class has:
- `append(entry)` — writes JSONL
- `read()` — returns list of dicts
- `count()` — returns count

**IMPORTANT**: The JSONL entries use `status` field with values from `TaskStatus` enum.
Check `models.py` for exact values. Known statuses: `approved`, `rejected`, `blocked`,
`error`, `done`, `in_progress`. The `actor` field uses `Actor` enum: `DEV`, `QA`.

However — the JSONL may not have explicit "rejected" entries from QA. QA reject is handled
by incrementing `_iterations_without_approval` but may not log a separate entry. Check the
actual JSONL to see what entries look like. Run:
```bash
cd /src/gis/hay-project
head -5 specs/arch/11-gis-renderer/99-todo.iterations.jsonl | python3 -m json.tool
```

If QA reject doesn't produce a JSONL entry, then count iterations between dev calls instead:
count the number of entries since the last QA approved entry for this task.

---

### Bug Fix 5: Fix blocked-despite-QA-approve

**Problem**: In the blocker triage flow (dev blocked → QA evaluates blocker), QA can approve
(the task was actually done in a previous session). But the code doesn't return immediately
after `_finalize_task()`, and a subsequent QA recovery exhaustion can produce a second blocked
entry that overwrites the approval.

**Location**: Around lines 2090-2160 (the blocker triage section after `if dev_response.status == "blocked":`)

**Fix**: After `_finalize_task()` in the QA-approve branch of blocker triage, add an explicit
`return`. Also ensure no code after it can produce another entry.

Look for the branch where `qa_response.decision == "approve"` inside the blocked-handling code.
It should have `self._finalize_task(phase, task)`. After that line, add `return`.

The current flow is approximately:
```python
# Blocker triage QA
qa_response = self._run_qa_with_recovery(...)  # for blocker triage
if qa_response.decision == "approve":
    self._finalize_task(phase, task)
    # BUG: no return here! Code falls through to...
elif qa_response.decision == "needs_user_input":
    ...
else:  # reject
    ...
```

Add `return` after `_finalize_task()` in the approve branch.

---

### Bug Fix 6: Dishonest recovery — improve prompts

**Problem**: When recovery retries after output truncation, the dev agent sometimes fabricates:
```json
{"status": "done", "summary": "Recovery: previous implementation already completed"}
```
with no actual code changes.

**Location**: Prompt constants at lines 143-197 in orchestrator.py.

**Fix**: Add anti-fabrication instruction to each recovery prompt.

`_RECOVERY_SUBTASK` (line 143):
Add to the end of the string:
```
CRITICAL: Do NOT claim the task is already done unless you have personally written or
verified the code in THIS session. If no code changes were made, report status "blocked"
with a description of what prevented progress, NOT status "done".
```

`_RECOVERY_SUMMARIZE` (line 160):
Add same anti-fabrication text.

`_RECOVERY_RESTART` (line 167):
Add same anti-fabrication text.

Also add to `_QA_RECOVERY_SUMMARIZE` (line 184) and `_QA_RECOVERY_RESTART` (line 191):
```
CRITICAL: Base your decision only on code you can verify exists. Do not approve based
on the developer's claim that work was done previously — check the actual file changes.
```

---

### Bug Fix 7: Validate "done" claims with VCS diff

**Problem**: Dev claims `status: done` but no files were actually modified.

**Location**: After dev_response is parsed in the feedback loop, before QA review.
Around line ~2090 (after the blocked-handling block ends, before "QA review" section).

**Fix**: Add a check after `dev_response.status == "done"` is confirmed:

```python
            # ── Validate "done" claim: check VCS diff ──
            if dev_response.status == "done" and self.vcs_backend is not None:
                vcs_diff = self._vcs_get_diff(task)
                if not vcs_diff or not vcs_diff.strip():
                    log.warning(
                        "dev.empty_diff_on_done",
                        task_label=task.label,
                        iteration=iteration,
                    )
                    self.ui.print_warning(
                        f"[{work_label}] ⚠️ Dev claims done but VCS diff is empty. "
                        f"Downgrading to blocked."
                    )
                    dev_response = DevResponse(
                        status="blocked",
                        summary="No file changes detected despite claiming done. "
                                "The task may already be complete from a previous session, "
                                "or no actual code was written.",
                        files_modified=[],
                        notes="Auto-downgraded from 'done' due to empty VCS diff.",
                    )
                    # Don't increment _consecutive_exhaustions here —
                    # the blocked handler below will do that
```

This needs to go BEFORE the `if dev_response.status == "blocked":` check so the downgraded
response enters the blocked flow naturally.

**Important**: Only applies when `self.vcs_backend is not None` (VCS is enabled). Without VCS,
there's no diff to check, so skip this validation.

Also handle the edge case where `dev_response.files_modified` is an empty list but files
were actually changed (e.g., the agent forgot to list them). The VCS diff is the ground truth.

---

## Implementation Order

1. **Bug Fix 3** — One-line constant change (easy win)
2. **Bug Fix 2** — Loop restructuring (most complex edit)
3. **Bug Fix 5** — Add `return` after finalize (simple)
4. **Bug Fix 7** — VCS diff validation (medium)
5. **Bug Fix 6** — Prompt improvements (copy-paste)
6. **Bug Fix 4** — Counter persistence (most complex new code)
7. **Tests** — Add tests for all fixes
8. **Commit** — Conventional commit messages

---

## Key File Locations Quick Reference

| Item | File | Line(s) |
|------|------|---------|
| Recovery prompts | orchestrator.py | 143-197 |
| Stuckness thresholds | orchestrator.py | 198-200 |
| Instance vars (counters) | orchestrator.py | 316-319 |
| Task counter reset | orchestrator.py | 460-466 |
| `_is_task_stuck()` | orchestrator.py | ~1211 |
| `_run_arch()` | orchestrator.py | ~1021 |
| `_apply_arch_decision()` | orchestrator.py | ~1107 |
| `_build_error_summary()` | orchestrator.py | ~908 |
| Feedback loop start | orchestrator.py | ~2010 |
| Stuckness check (top of loop) | orchestrator.py | ~2022 |
| Dev with recovery | orchestrator.py | ~2060 |
| Dev blocked handler | orchestrator.py | ~2070 |
| Blocker triage QA | orchestrator.py | ~2090-2160 |
| QA review | orchestrator.py | ~2210-2300 |
| QA reject (bottom of loop) | orchestrator.py | ~2270-2295 |
| `_finalize_task()` | orchestrator.py | ~800 |
| `_vcs_get_diff()` | orchestrator.py | ~771 |
| `_vcs_commit_task()` | orchestrator.py | ~791 |
| `IterationLog.read()` | log.py | ~35 |
| `TaskStatus` enum | models.py | ~25 |
| `Actor` enum | models.py | ~15 |
| `ArchResponse` | models.py | ~440 |
| `DevResponse` | models.py | ~80 |
| `QARequest` | models.py | ~120 |

---

## Test Strategy

All tests go in `tests/test_dryrun.py`. Run with:
```bash
cd /src/gis/tools/tasker && uv run python tests/test_dryrun.py
```

Tests needed:
1. **test_stuckness_counts_all_blocked**: Mock dev returning blocked, verify counter increments
   regardless of summary content (no "multiple recovery attempts" guard).
2. **test_stuckness_threshold_lowered**: Verify ARCH triggers at 2 consecutive exhaustions, not 3.
3. **test_stuckness_check_deferred**: Verify `_is_task_stuck()` is evaluated at bottom of loop,
   not top. Mock counters exceeding threshold, verify `_pending_arch_check` is set.
4. **test_stuckness_restored_from_log**: Create a mock JSONL with blocked entries, verify
   `_restore_stuckness_from_log()` computes correct counters.
5. **test_blocked_despite_qa_approve_fix**: Simulate blocker triage where QA approves,
   verify `_finalize_task` is called and function returns immediately.
6. **test_empty_diff_downgrades_to_blocked**: Mock VCS returning empty diff, verify
   dev "done" is downgraded to "blocked".
7. **test_recovery_prompts_anti_fabrication**: Verify prompt constants contain anti-fabrication text.
