# JJ Integration — Option B: Checkpointing with Squash

> **Status**: Documented for future implementation. Currently using Option A (single-commit per task).

## Overview

Option B allows the developer agent to create intermediate `jj commit` checkpoints during
long tasks. This provides crash recovery and lets QA see incremental progress. On QA approval,
all checkpoints are squashed into a single clean commit.

## Workflow

```
1. QA/Orchestrator:  jj new <parent_change_id> -m "Task: <description>"
   Store: task.base_change = <parent>, task.description = "<description>"

2. Developer loop (with checkpointing):
   - Dev writes code
   - jj commit -m "checkpoint: add user model"     # safety checkpoint
   - Dev writes more code
   - jj commit -m "checkpoint: add login endpoint"  # safety checkpoint

3. QA review loop:
   - jj diff --from <task.base_change>              # sees ALL changes regardless of checkpoints
   - QA approves / rejects / requests user input

4. On QA approval:
   - Squash all checkpoints into the task change
   - Restore the clean task description
```

## Key Implementation Details

### Non-interactive jj commands

ALL jj commands MUST use `-m` flag to avoid opening `$EDITOR`:

```bash
jj new <parent> -m "Task: description"    # NOT: jj new <parent> (opens editor)
jj commit -m "checkpoint: doing X"        # NOT: jj commit (opens editor)
jj describe -m "Task: description"        # NOT: jj describe (opens editor)
```

### Squash procedure

After QA approves with a stack of checkpoints:

```
Before:  base ──○(Task desc)──○(cp1)──○(cp2)──○(cp3)──@(empty)

Step 1: Squash each checkpoint bottom-up into the task change:
  for cp in [cp3, cp2, cp1]:           # top to bottom
    jj squash -r <cp_change_id>        # merges cp into its parent

Step 2: Restore the original task description:
  jj describe -r <task_change_id> -m "<original task description>"

After:   base ──○(Task desc with all changes)──@(empty)
```

**Gotcha**: `jj squash -r <rev>` replaces the parent's description with the child's.
So after squashing checkpoints, you MUST `jj describe` to restore the original task message.

### Recovery from crash

If the developer goose session crashes:
1. Find the last checkpoint: `jj log`
2. The working copy (`@`) may have partial changes — those are preserved by jj
3. Resume the dev session with `--name` (goose auto-resumes)
4. Dev continues from where it left off

### QA diff is always complete

`jj diff --from <base_change>` shows ALL changes in the task stack, regardless of
how many checkpoints exist. QA always sees the full picture.

## Comparison with Option A

| Aspect | Option A (current) | Option B (this doc) |
|--------|-------------------|---------------------|
| Dev workflow | Just write code, no commits | `jj commit` between steps |
| QA review | `jj diff --from <base>` | Same — always sees full diff |
| On approve | `jj commit` (trivial) | Squash loop + `jj describe` |
| History | Perfectly clean | Perfectly clean (after squash) |
| Crash recovery | Lost work since last task | Recover from last checkpoint |
| Complexity | Minimal | Slightly more orchestration |
| Risk of editor popup | None (`jj commit -m`) | None if `-m` always used |

## When to switch to Option B

Consider enabling Option B when:
- Tasks consistently take > 10 minutes
- Developer goose sessions crash or timeout frequently
- You want QA to review incremental progress mid-task
- The codebase is large and losing partial work is expensive

## Implementation Sketch

```python
# In jj.py — checkpoint-aware methods

def commit_checkpoint(description: str) -> str:
    """Create a jj checkpoint during development."""
    return _run_jj(["commit", "-m", f"checkpoint: {description}"])

def squash_checkpoints(task_change_id: str, original_description: str) -> None:
    """Squash all checkpoints above task_change_id into it."""
    # Get all descendants of task_change_id (excluding @ which is empty)
    descendants = _run_jj(["log", "--no-graph", "-T", "change_id", "-r", f"ancestors(@) & {task_change_id}+"])
    # Skip the first one (that's task_change_id itself), squash the rest bottom-up
    checkpoints = descendants.strip().split("\n")[1:]  # reverse order
    for cp in reversed(checkpoints):  # bottom-up
        _run_jj(["squash", "-r", cp])
    # Restore clean description
    _run_jj(["describe", "-r", task_change_id, "-m", original_description])
```
