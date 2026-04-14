# AGENTS.md — Code Agent Guide for `tasker`

> This file provides essential context for any AI code agent working on this repository.
> Read it before making changes.

## Project Overview

**tasker** is a CLI tool that orchestrates a **QA ↔ Developer feedback loop** using [Goose](https://github.com/block/goose) agents. It reads a markdown task list, assigns tasks to a Developer agent, sends the output to a QA Reviewer agent, and loops on rejection until approved. It supports interactive chat for resolving blockers, graceful error recovery, and optional [Jujutsu (jj)](https://github.com/jj-vcs/jj) version control integration.

**License**: Apache 2.0

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python ≥ 3.11 |
| CLI framework | [Typer](https://typer.tiangolo.com/) |
| Terminal UI | [Rich](https://rich.readthed.me/) |
| Config format | YAML (recipes), Markdown (task lists), JSONL (logs) |
| Package manager | [uv](https://docs.astral.sh/uv/) |
| Build backend | Hatchling |
| VCS (optional) | Jujutsu (`jj`) |

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
│   ├── goose.py              # Goose subprocess runner + JSON extraction
│   ├── orchestrator.py       # Core QA↔Dev loop, recovery, chat mode, jj integration
│   ├── jj.py                 # Jujutsu VCS helpers (new, commit, diff, log)
│   ├── log.py                # Append-only JSONL iteration logger
│   └── ui.py                 # Rich Live UI (progress bars, iteration table, chat input)
├── tests/
│   ├── fixtures/
│   │   ├── sample_tasks.md   # Test task file (basic ## phases)
│   │   ├── e2e_test.md       # E2E test task file
│   │   └── subphase_tasks.md # Test task file with ### sub-phases
│   └── test_dryrun.py        # 17 unit/integration tests (no goose subprocess)
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
      ├── ui.py              (Rich live display)
      ├── models.py          (all data types)
      └── jj.py              (optional VCS integration)

goose.py          ← standalone, only depends on stdlib
parser.py         ← depends on models.py
log.py            ← depends on models.py
models.py         ← standalone, no internal deps
jj.py             ← standalone, only depends on stdlib
ui.py             ← depends on models.py
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
`NORMAL (1×) → CONTINUE (3×) → SUBTASK (3×) → SUMMARIZE (3×)`

If all stages are exhausted, a synthetic `blocked` response is created and sent to QA for triage.

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

### 6. JJ Integration (`--jj` flag)

When enabled, each task gets an isolated `jj new` change. On QA approval, it's committed as a single clean commit. The diff is injected into the QA prompt as `project_context`.

## Common Tasks

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
- JJ failures are wrapped in `JJResult` — always check `.success` before using `.stdout`
- Malformed agent output triggers recovery stages, never crashes

### Logging
- Use `logging.getLogger(__name__)` for debug-level logging (e.g. in `jj.py`)
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
2. **Do NOT let agents run version control commands** — jj/git operations are handled by the orchestrator
3. **Do NOT add parameters to recipes without updating `to_params()`** in the corresponding model class — all declared recipe params must be provided every time
4. **Do NOT modify `uv.lock` manually** — use `uv add`/`uv remove`
5. **Do NOT break the JSON response contract** — agents MUST return JSON with `status` (dev) or `decision` (QA) keys, and parsers MUST reject unknown values (see `_parse_dev_response` / `_parse_qa_response` strictness)

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
- JJ module (availability check, command execution, graceful failure)
- Timeout feedback message generation
- Session scope (enum, scope key computation, `Task.subphase` field, backward compatibility)

When adding new features, add corresponding dry-run tests — no real Goose subprocess calls.
