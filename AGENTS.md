# AGENTS.md — Code Agent Guide for `tasker`

> This file provides essential context for any AI code agent working on this repository.
> Read it before making changes.

## Project Overview

**tasker** is a CLI tool that orchestrates a **QA ↔ Developer feedback loop** using [Goose](https://github.com/block/goose) agents. It reads a markdown task list, assigns tasks to a Developer agent, sends the output to a QA Reviewer agent, and loops on rejection until approved. It supports interactive chat for resolving blockers, graceful error recovery, and optional version control integration (Jujutsu or Git).

**License**: Apache 2.0

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python ≥ 3.11 |
| CLI framework | [Typer](https://typer.tiangolo.com/) |
| Terminal UI | [Rich](https://rich.readthed.me/) |
| Config format | YAML (recipes), Markdown (task lists), JSONL (logs) |
| Monitoring | [structlog](https://www.structlog.org/) (structured log file + console) |
| Package manager | [uv](https://docs.astral.sh/uv/) |
| Build backend | Hatchling |
| VCS (optional) | Jujutsu (`jj`) or Git (feature branch + squash merge) |

## Repository Structure

```
tools/tasker/
├── pyproject.toml            # Project metadata, dependencies, entry point
├── uv.lock                   # Lock file (do not hand-edit)
├── AGENTS.md                 # ← You are here
├── README.md                 # Comprehensive user documentation
├── LICENSE                   # Apache 2.0
├── recipes/
│   ├── recipe-dev.yaml       # Developer agent system prompt + parameters
│   └── recipe-qa.yaml        # QA reviewer agent system prompt + parameters
├── src/tasker/
│   ├── __init__.py           # Package marker, version
│   ├── __main__.py           # `python -m tasker` entry point
│   ├── main.py               # Typer CLI app definition
│   ├── models.py             # All dataclasses & enums (Task, Phase, payloads, RecoveryStage, SessionScope)
│   ├── parser.py             # Markdown task-list parser (Phase/Task extraction)
│   ├── goose.py              # Goose subprocess runner, JSON extraction, heartbeat thread
│   ├── orchestrator.py       # Core QA↔Dev loop, recovery, chat mode, VCS integration
│   ├── jj.py                 # Backward-compat re-exports (see vcs/jj_backend.py)
│   ├── log.py                # Append-only JSONL iteration logger
│   ├── monitoring.py         # structlog configuration + setup (observability)
│   ├── ui.py                 # Rich Live UI (progress bars, iteration table, chat input, activity indicators)
│   └── vcs/
│       ├── __init__.py       # VCSBackend protocol + create_backend() factory
│       ├── jj_backend.py     # Jujutsu VCS backend (jj new/commit/diff)
│       └── git_backend.py    # Git VCS backend (feature branch + squash merge)
├── tests/
│   ├── fixtures/
│   │   ├── sample_tasks.md   # Test task file (basic ## phases)
│   │   ├── e2e_test.md       # E2E test task file
│   │   └── subphase_tasks.md # Test task file with ### sub-phases
│   └── test_dryrun.py        # 47 unit/integration tests (no goose subprocess)
└── docs/
    └── jj-option-b.md        # Future design doc (not yet implemented)
```

## Module Dependency Graph

```
main.py
 └── orchestrator.py (core pipeline)
      ├── goose.py           (subprocess runner)
      ├── parser.py          (markdown → Phase/Task)
      ├── log.py             (JSONL logger)
      ├── monitoring.py      (structlog setup, called once from main.py)
      ├── ui.py              (Rich live display)
      ├── models.py          (all data types)
      └── vcs/               (optional VCS integration)
           ├── __init__.py   (VCSBackend protocol, create_backend factory)
           ├── jj_backend.py (Jujutsu backend)
           └── git_backend.py(Git backend)

goose.py          ← depends on structlog (subprocess lifecycle, heartbeat thread)
parser.py         ← depends on models.py, structlog (parse events)
log.py            ← depends on models.py
monitoring.py     ← depends on structlog (configures root logger)
models.py         ← standalone, no internal deps
jj.py             ← backward-compat shim, re-exports from vcs.jj_backend
ui.py             ← depends on models.py, structlog (lifecycle events, activity indicators, pending iterations)
```

## Key Architecture Concepts

### 1. Agent Communication Protocol

All communication between the orchestrator and Goose agents uses **structured JSON** passed via `--params KEY=VALUE` on the CLI and returned in the agent's last assistant message.

**Dev → Orchestrator** (extracted from agent output):
```json
{"status": "done"|"blocked", "summary": "...", "files_modified": [...], "notes": "...",
 "blocker_description": "...", "blocker_suggestion": "..."}
```

**QA → Orchestrator** (extracted from agent output):
```json
{"decision": "approve"|"reject"|"needs_user_input", "feedback": "...",
 "concerns": [...], "user_question": "..."}
```

### 2. Graceful Degradation (Recovery Stages)

When the Developer returns malformed output, the orchestrator escalates through stages:
`NORMAL (1×) → CONTINUE (3×) → SUBTASK (3×) → SUMMARIZE (3×) → RESTART (1×)`

Each stage sends a progressively more directive recovery instruction to the agent. The **RESTART** stage also rotates the dev session to a fresh Goose session, clearing all accumulated stale context so the agent can approach the task from scratch.

If all stages are exhausted, a synthetic `blocked` response is created and sent to QA for triage.

**QA recovery** follows the same pattern but with fewer stages (QA doesn't have a subtask equivalent):
`NORMAL (1×) → CONTINUE (3×) → SUMMARIZE (3×) → RESTART (1×)`

- `CONTINUE` — "Keep your review focused, put findings in the JSON block"
- `SUMMARIZE` — "Stop investigating, just output the JSON decision block"
- `RESTART` — Fresh QA session, review from scratch

The **RESTART** stage rotates the QA session. If all stages are exhausted, a synthetic `reject` response is created so the developer gets another chance to iterate.

### 3. Interactive Chat Mode

When QA returns `needs_user_input`, the Live UI pauses and the user types answers. QA processes responses in a loop until resolved (`/done`), skipped (`/skip`), or QA approves.

### 4. Session Persistence & Scope

Each `tasker` run generates unique session names (e.g. `dev_20260413_111500_a1b2c3`). The `--name` flag makes Goose auto-resume existing sessions, so agents accumulate context across tasks within a run.

**Session scope** (`--session-scope`) controls when new sessions are created to prevent context overflow:

| Scope | Boundary | Behavior |
|-------|----------|----------|
| `phase` | `##` heading | One session per phase — most context, risk of overflow on large phases |
| `subphase` (default) | `###` heading | One session per sub-phase group — good balance |
| `task` | `- [ ]` item | Fresh session every task — no overflow risk, no cross-task context |

The orchestrator computes a **scope key** for each task (e.g. `"P1::P1-2 API Endpoints"` for subphase scope) and generates new dev/qa session IDs whenever the key changes.

**Manual override** (`--new-session`): A one-shot flag that forces a new session on the very next task, regardless of scope. Useful when the agent is getting slow/confused mid-subphase.

### 5. Sub-phase Parsing

The parser recognizes `###` headings within `##` phases and records the sub-phase name on each `Task` via the `subphase` field. This is used by the session scope system and is backward-compatible — task files without `###` headings work unchanged.

### 6. VCS Integration (`--vcs` flag)

When enabled (`--vcs jj` or `--vcs git`), each task gets an isolated workspace for version control. On QA approval, the orchestrator marks the task `[x]` in the markdown, then commits all changes as a single clean commit. The diff is injected into the QA prompt as `project_context`.

Both backends implement the `VCSBackend` protocol (`vcs/__init__.py`):
- `init()` — capture the current state as the starting base
- `begin_task()` — create an isolated workspace for the task
- `get_diff()` — return the diff for QA review
- `commit_task()` — finalize changes as a permanent commit

**JJ backend**: Uses `jj new` for task isolation, `jj diff` for QA context, `jj commit` for finalization. Produces a linear history.

**Git backend**: Uses feature branches (`git checkout -b`), `git diff` for QA context, and `git merge --squash` for finalization. Before switching branches, unstaged changes (like the `[x]` markdown mark) are captured into the feature branch to prevent data loss.

The `jj.py` module is a backward-compatibility shim that re-exports from `vcs.jj_backend` — new code should import from `tasker.vcs` directly.

The `jj.py` module is a backward-compatibility shim that re-exports from `vcs.jj_backend` — new code should import from `tasker.vcs` directly.

### 7. UI Activity Indicators & Live Feedback

When a dev or QA goose subprocess is running, the UI provides two layers of real-time feedback:

**Header panel** (`_ActivityRenderable`): Shows the actor icon, name, and task label with a pulsing elapsed-time counter. Managed via `ui.activity_start(label)` / `ui.activity_stop()`. The `_run_goose_with_ui()` wrapper calls these automatically around every `run_goose()` invocation.

**Iteration table pending row** (`_PendingIteration`): Shows an animated braille spinner (`⣾⣽⣻⢿⡿⣟⣯⣷`) in the Status column with elapsed time, making it clear that the orchestrator is waiting for a subprocess. Managed via `ui.set_pending_iteration(actor, task_label)` / `ui.clear_pending_iteration()`. The `_run_goose_with_ui()` wrapper calls these as well.

**Heartbeat thread** (`goose.py`): A background daemon thread emits `goose.heartbeat` log events every 30 seconds during subprocess waits, so the monitor log shows liveness even during long-running agent calls.

**Iteration log completeness**: All iteration entries are now added to the UI table — not just successful dev responses and QA decisions. Error entries (timeout, subprocess_failed, malformed_output, blocked) and special events (max_iterations_reached, needs_user_input) all appear with human-readable summaries via `_entry_summary()` (e.g. "⏱ Timeout after 600s", "💥 Subprocess failed (rc=-1)", "⚠ Malformed JSON (stage=continue)").

### 8. `_run_goose_with_ui()` Wrapper

All goose subprocess calls go through `_run_goose_with_ui()` (in `orchestrator.py`), which wraps `run_goose()` with:
- `activity_start()` / `activity_stop()` — header panel elapsed timer
- `set_pending_iteration()` / `clear_pending_iteration()` — animated table row

There are 4 call sites: chat loop QA, dev agent, QA blocker triage, and QA review. Never call `run_goose()` directly from the orchestrator — always use the wrapper.

### Running Tests

```bash
cd tools/tasker
uv run python tests/test_dryrun.py
```

Tests are **dry-run** — they mock the Goose subprocess and test parser, logger, models, JSON extraction, command builder, and jj module in isolation.

### Running the CLI

```bash
cd tools/tasker
uv run tasker --dev recipes/recipe-dev.yaml --qa recipes/recipe-qa.yaml <task_file.md>
```

### Adding a Dependency

```bash
cd tools/tasker
uv add <package>
```

This updates both `pyproject.toml` and `uv.lock`.

## Coding Conventions

### Style
- **Python 3.11+** features are fine (e.g. `X | Y` union types, `from __future__ import annotations`)
- Use **dataclasses** for data models (see `models.py`)
- Use **str enums** for enumerated types (see `Actor`, `TaskStatus`, `RecoveryStage`)
- Type hints are required on all public function signatures
- Docstrings use Google-style on modules and public functions

### Imports
- `from __future__ import annotations` at the top of every module
- Absolute imports within the package: `from .models import Task` (relative) or `from tasker.models import Task` (absolute)
- Stdlib imports before third-party before local

### Error Handling
- Never let unhandled exceptions escape to the user — catch and log with `self.ui.print_error()`
- Goose subprocess failures are wrapped in `GooseRunResult` (never raise from `run_goose()`)
- VCS failures are caught at the orchestrator level — `RuntimeError` from backends is logged as a warning and VCS is disabled for the run
- Malformed agent output triggers recovery stages, never crashes

### Logging
- **Structured monitoring** via `structlog` — configured once in `monitoring.py:setup_monitoring()`, called from `main.py` before orchestrator creation
- All modules use `structlog.get_logger(__name__)` to emit structured key-value events (e.g. `log.info("task.starting", task_label="P1.T1", phase="Phase 1")`)
- Existing `logging.getLogger(__name__)` calls (VCS backends) also flow through structlog's stdlib integration
- **Two log files serve different purposes:**
  - `tasker.log` (monitor log) — **everything**: orchestration decisions, session rotations, recovery escalations, subprocess launches, VCS ops, parser events, UI lifecycle. Human-readable, key-value format.
  - `<task_file>.iterations.jsonl` (iteration log) — **QA↔Dev exchanges only**: structured JSON records of each agent call's result. Machine-parseable.
- User-facing messages go through `TaskerUI` methods (`print_info`, `print_warning`, `print_error`, `print_success`)
- All QA↔Dev exchanges are recorded in the JSONL log via `IterationLog.append()`

### File Paths
- Always use `Path` objects, never raw strings for file paths
- Resolve to absolute paths before passing to subprocesses: `path.resolve()`
- The working directory for Goose agents defaults to the task file's parent directory

### Recipes (YAML)
- Recipe parameters must be declared in the `parameters:` section with defaults
- All declared parameters must always be provided in `to_params()` (goose validates them)
- Prompt templates use Jinja-style `{{ variable }}` syntax with `{% if %}` conditionals
- `--params KEY=VALUE` values have newlines escaped as `\n` automatically

## Important Constraints

1. **Do NOT use `--session-id` or `--resume` with Goose** — use `--name` for session persistence
2. **Do NOT add parameters to recipes without updating `to_params()`** in the corresponding model class — all declared recipe params must be provided every time
4. **Do NOT modify `uv.lock` manually** — use `uv add`/`uv remove`
5. **Do NOT break the JSON response contract** — agents MUST return JSON with `status` (dev) or `decision` (QA) keys, and parsers MUST reject unknown values (see `_parse_dev_response` / `_parse_qa_response` strictness)
6. **Do NOT reorder `_finalize_task`** — the sequence `mark_task_done → update_markdown → _vcs_commit_task` is critical. The markdown `[x]` must be on disk before the VCS commit runs, or it will be lost on branch switches (git) or excluded from the commit.
7. **Do NOT let agents run version control commands** — VCS operations are handled by the orchestrator via the `VCSBackend` protocol

### 9. Structured Monitoring (`--monitor-log`)

The `--monitor-log` flag controls a separate structured log file that captures **all orchestration events** (not just QA↔Dev exchanges). This complements the JSONL iteration log.

```bash
# Default: tasker.log in the task file's parent directory
uv run tasker --dev ... --qa ... specs/arch/99-todo.md

# Custom path
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --monitor-log /tmp/debug.log

# Disable file logging (console only)
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --no-monitor-log
```

**Controlling log levels** (`--log-level` / `--file-log-level`):

The console and file handlers have independent log levels. Defaults are `WARNING` for console (quiet terminal) and `DEBUG` for the file (capture everything).

```bash
# Console: only warnings+  |  File: debug (everything) — defaults
uv run tasker --dev ... --qa ... specs/arch/99-todo.md

# Verbose console — see all info+ in the terminal
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --log-level info

# Quiet file — only warnings+ written to disk
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --file-log-level warning

# Debug everything everywhere
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --log-level debug

# Invalid level → clear error message and exit
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --log-level trace
# Error: Unknown log level 'trace'. Must be one of: critical, debug, error, warning
```

Accepted values (case-insensitive): `debug`, `info`, `warning` (or `warn`), `error`, `critical` (or `crit`).

**Log event taxonomy** (dot-separated namespace):

| Event | Module | Level | When |
|-------|--------|-------|------|
| `orchestrator.starting` | orchestrator | info | Run begins (sessions, config) |
| `orchestrator.finished` | orchestrator | info | Run ends (summary stats) |
| `tasks.loaded` | orchestrator | info | After parsing task file |
| `task.starting` | orchestrator | info | Before dev agent call |
| `task.finalizing` | orchestrator | info | QA approved, marking done |
| `task.finalized` | orchestrator | info | Task fully complete |
| `feedback_loop.start` | orchestrator | info | QA↔Dev loop begins for a task |
| `feedback_loop.max_iterations` | orchestrator | error | Hit iteration limit |
| `session.rotated` | orchestrator | info | New dev/qa sessions created (reason: `scope_boundary`, `force_new_session`, or `recovery_restart`) |
| `dev.recovery_start` | orchestrator | info | Recovery loop begins |
| `dev.call` | orchestrator | debug | Before goose subprocess launch |
| `dev.response_parsed` | orchestrator | info | Dev returned valid JSON |
| `dev.done` | orchestrator | info | Dev status=done |
| `dev.blocked` | orchestrator | warning | Dev status=blocked |
| `dev.malformed_output` | orchestrator | warning | Dev JSON unparsable |
| `dev.escalating` | orchestrator | warning | Recovery stage advancing |
| `dev.recovery_exhausted` | orchestrator | error | All recovery stages failed |
| `dev.timeout` | orchestrator | warning | Dev process killed |
| `dev.subprocess_failed` | orchestrator | error | Dev process crashed |
| `qa.call` | orchestrator | debug | Before QA goose subprocess |
| `qa.response_parsed` | orchestrator | info | QA returned valid JSON |
| `qa.approved` | orchestrator | info | QA decision=approve |
| `qa.rejected` | orchestrator | info | QA decision=reject |
| `qa.needs_user_input` | orchestrator | info | QA needs user chat |
| `qa.malformed_output` | orchestrator | warning | QA JSON unparsable |
| `qa.escalating` | orchestrator | warning | QA recovery stage advancing |
| `qa.recovery_start` | orchestrator | info | QA recovery loop begins |
| `qa.recovery_exhausted` | orchestrator | error | All QA recovery stages failed |
| `qa.timeout` | orchestrator | warning | QA process killed |
| `qa.subprocess_failed` | orchestrator | error | QA process crashed |
| `vcs.initialized` | orchestrator | info | VCS backend init success |
| `vcs.task_started` | orchestrator | info | Task workspace created |
| `vcs.diff_obtained` | orchestrator | debug | Diff retrieved for QA |
| `vcs.task_committed` | orchestrator | info | Task changes committed |
| `vcs.begin_task_failed` | orchestrator | warning | VCS workspace creation failed |
| `vcs.commit_failed` | orchestrator | error | VCS commit failed |
| `goose.launching` | goose | debug | Before subprocess.Popen |
| `goose.completed` | goose | info | Subprocess finished |
| `goose.timeout` | goose | warning | Process killed on timeout |
| `goose.launch_failed` | goose | error | Failed to start process |
| `parser.parsing` | parser | debug | Starting file parse |
| `parser.parsed` | parser | info | Parse complete (counts) |
| `parser.updating_markdown` | parser | debug | Rewriting checkboxes |
| `jj.run` | vcs/jj_backend | debug | JJ subprocess command |
| `git.run` | vcs/git_backend | debug | Git subprocess command |
| `goose.heartbeat` | goose | debug | Periodic liveness ping during subprocess wait (every 30s) |
| `ui.live_started` | ui | debug | Rich Live display started |
| `ui.live_stopped` | ui | debug | Rich Live display stopped |
| `ui.live_paused` | ui | debug | Live paused for chat input |
| `ui.live_resumed` | ui | debug | Rich Live resumed after chat |
| `ui.activity_started` | ui | debug | Header panel activity indicator started |
| `ui.activity_stopped` | ui | debug | Header panel activity indicator stopped |
| `ui.pending_started` | ui | debug | Animated pending row added to iteration table |

**When adding new log events:** Use dot-separated names (e.g. `module.action`), include `task_label` when available, and choose the appropriate level (`debug` for verbose/noisy, `info` for milestones, `warning` for recoverable issues, `error` for failures).

## Environment Variables

The orchestrator sets these for every Goose invocation (see `goose.py`):

| Variable | Value | Purpose |
|---|---|---|
| `GOOSE_CONTEXT_STRATEGY` | `summarize` | Compress long session context |
| `GOOSE_AUTO_COMPACT_THRESHOLD` | `0.35` | Auto-compact at 35% of context window |

## Testing Philosophy

Tests in `test_dryrun.py` cover:
- Markdown parser (phase/task extraction, checkbox toggling, `###` sub-phase parsing)
- JSONL logger (append, read, count)
- JSON extraction from agent output (fenced blocks, raw JSON, last-brace fallback)
- Command builder (params, escaping, flags)
- All model classes (dataclass construction, `to_params()`, `to_dict()`)
- Response parser strictness (rejects unknown status/decision values)
- Goose envelope extraction (JSON `{"messages": [...]}` unwrapping)
- VCS backends (protocol compliance, init validation, helper functions for both jj and git)
- Task VCS fields (`vcs_description`, `base_ref`, `task_ref`)
- Timeout feedback message generation
- Session scope (enum, scope key computation, `Task.subphase` field, backward compatibility)
- `_finalize_task` ordering invariant (markdown [x] must be on disk before VCS commit)
- Monitoring setup (structlog configuration, file output, idempotency, get_logger helper)
- Monitoring integration (parser events captured in monitor log, orchestrator task lifecycle captured)
- UI activity indicators (`_ActivityRenderable`, header panel start/stop, elapsed timer)
- Goose heartbeat thread (background liveness pings, structlog stdlib integration)
- `_run_goose_with_ui` wiring (activity start/stop, pending iteration lifecycle)
- Iteration table entries (`_format_timestamp`, `_entry_summary`, pending row with braille spinner)

When adding new features, add corresponding dry-run tests — no real Goose subprocess calls.
