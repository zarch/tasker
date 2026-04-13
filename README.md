# tasker

Goose-based task orchestration CLI with a QA/Dev feedback loop, interactive issue resolution, graceful error recovery, and optional Jujutsu (jj) version control integration.

`tasker` reads a markdown task list, assigns each task to a **Developer** goose agent, then sends the result to a **QA Reviewer** goose agent. If QA rejects the work, feedback is routed back to the developer in a loop until the task is approved — then the next task begins. When agents encounter blockers or return malformed output, `tasker` escalates gracefully through multiple recovery strategies, including an interactive chat mode where the user can resolve ambiguities directly.

## How it works

```
┌─────────┐      task P1.T1       ┌──────────┐
│         │ ──────────────────►    │          │
│   QA    │                        │  Dev     │
│ Reviewer│  ◄──────────────────   │ Agent    │
│         │   implementation      │          │
└─────────┘                        └──────────┘
     │         ▲                        │
     │         │                        │
     │ approve │ blocked                │ status=blocked
     │ → done  │ → triage               │ → QA triage
     │ reject  │                        │
     │ → feedback                        │ done → QA review
     ▼         │                        ▼
  next task    │                   re-implement
               │
          needs_user_input
               │
               ▼
        ┌──────────────┐
        │  💬 Chat     │ ← user types answers
        │  with User   │   QA processes responses
        └──────────────┘
               │
          resolved → dev retries
          /skip → mark done, next task
```

### Normal flow

1. **Parser** reads the markdown file and extracts phases/tasks.
2. **Orchestrator** picks the first incomplete task and sends it to the Developer.
3. **Developer** (goose agent) implements the task, returns a JSON status report.
4. **QA Reviewer** (goose agent) inspects the code and returns approve/reject.
5. If rejected, feedback loops back to the Developer. If approved, the task is marked `[x]` in the markdown and the next task starts.

### Error handling flows

#### Dev blocked → QA triage

When the Developer returns `"status": "blocked"` (unclear requirements, missing specs, unknown dependencies):

1. The blocker description and the developer's suggestion are sent to QA.
2. QA checks the project's specification files to see if the answer exists.
3. If QA can resolve it from docs → returns `"reject"` with guidance, dev retries.
4. If QA can't resolve it → returns `"needs_user_input"` with a specific question → triggers **interactive chat**.

#### Interactive chat mode

When QA returns `"decision": "needs_user_input"`, the pipeline pauses and enters chat mode:

1. The Live UI is paused (so `input()` works).
2. QA's question is displayed to the user.
3. The user types a response → sent to the QA agent for processing.
4. QA can: ask follow-up questions (`needs_user_input`), give dev guidance (`reject`), or accept (`approve`).
5. The loop continues until QA approves or the user types `/done` (resolved) or `/skip` (move on).
6. After chat resolves, the pipeline resumes with the dev retrying (or task marked done).

#### Graceful degradation (malformed goose output)

When the Developer agent returns output that can't be parsed (no valid JSON with `status` key), the orchestrator escalates through recovery stages:

| Stage | Attempts | Instruction to dev |
|-------|----------|--------------------|
| NORMAL | 1 | Standard task prompt |
| CONTINUE | 3 | "Continue from where you left off and respond with JSON" |
| SUBTASK | 3 | "Break into subtasks, implement one, respond with JSON" |
| SUMMARIZE | 3 | "Stop implementing, summarize progress, respond with JSON `blocked`" |

If all stages are exhausted, a synthetic `blocked` response is generated and sent to QA for triage (which may trigger interactive chat).

## How it talks to goose

`tasker` invokes `goose run` as a subprocess for each agent turn. Key CLI details:

- **`--name <session>`** — names the goose session. Reusing the same name across invocations gives agents persistent context (goose auto-resumes).
- **`--recipe <path>`** — loads a YAML recipe that defines the agent's system prompt, extensions, and parameterized prompt template. Mutually exclusive with `--text`.
- **`--params KEY=VALUE`** — passes task data into recipe template variables (`{{ key }}`). Newlines and special characters are escaped automatically.
- **`--output-format json`** — goose returns a JSON envelope `{"messages": [...]}`. `tasker` extracts the last assistant message text and parses the structured JSON response from it.
- **`--max-turns N`** — limits how many tool calls the agent can make per invocation.
- **`--with-builtin developer`** — ensures the developer extension (file ops, shell) is available.

> ⚠️ `--session-id` requires `--resume` and is not used here. `--name` provides session persistence without that constraint.

## Installation

```bash
cd tools/tasker
uv sync
```

## Usage

```bash
cd tools/tasker

# Full run — all tasks
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md

# Start from a specific phase (1-based), earlier phases marked done
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --start-phase 3

# Custom model/provider
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --model claude-sonnet-4-20250514 \
              --provider anthropic

# Custom iteration log location
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --log output/iterations.jsonl

# With Jujutsu (jj) integration — each task gets its own commit
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --jj
```

## CLI Options

| Option | Default | Description |
|--------|---------|-------------|
| `--dev` | *(required)* | Path to the developer goose recipe (YAML) |
| `--qa` | *(required)* | Path to the QA goose recipe (YAML) |
| `task_file` | *(required)* | Path to the markdown task list |
| `--log` | `<task_file>.iterations.jsonl` | JSONL iteration log path |
| `--max-iterations` | `10` | Max QA↔Dev rounds per task before skipping |
| `--max-turns` | `80` | Max goose agent turns per invocation |
| `--timeout` | `600` | Timeout (seconds) per goose run. Process is killed and relaunched with context on timeout. |
| `--model` | *(goose default)* | Override goose model |
| `--provider` | *(goose default)* | Override goose provider |
| `--start-phase` | *(none)* | Start from phase N (1-based) |
| `--jj` | *(off)* | Enable Jujutsu (jj) integration for task-scoped version control |

## Jujutsu (jj) integration

When `--jj` is enabled, `tasker` creates one clean commit per task using [Jujutsu](https://github.com/jj-vcs/jj). The project directory must already be a jj repository (initialized with `jj git init`).

### Workflow (Option A — single commit per task)

```
base ──► jj new "Task: P1.T1" ──► dev implements ──► QA reviews jj diff ──► jj commit ──► next task
```

1. **Task begin**: `jj new <parent_change> -m "P1.T1: <task text>"` creates an isolated working-copy change.
2. **Developer** implements the task (no version control commands needed — jj tracks everything automatically).
3. **QA review**: `jj diff --from <base_change>` is computed and injected into the QA prompt as `project_context`, so QA sees exactly what changed.
4. **Task approved**: `jj commit -m "P1.T1: <task text>"` finalizes the change into a single clean commit.
5. **Task rejected**: The working-copy change is reused — the developer's next iteration builds on the same change. On approval, only the final state is committed.
6. **Next task**: `jj new <committed_change>` chains from the previous task's commit.

The result is a **linear history with one meaningful commit per task**, no intermediate checkpoints.

> **Important**: All jj commands use the `-m` flag to avoid opening `$EDITOR`. No manual intervention is ever required.

### Requirements

- `jj` must be installed and on `PATH`
- The working directory must be a jj repository (`jj git init` or colocated with `.jj/`)
- At least one base commit must exist before running tasker with `--jj`

## Task file format

Markdown files with `## Phase N` headings and `- [ ]` / `- [x]` checkboxes:

```markdown
## Phase 1 — MVP

- [ ] Create project workspace with Cargo.toml
- [ ] Implement core geometry types
- [x] Set up CI pipeline

## Phase 2 — Indexing

- [ ] Add R-Tree spatial index
- [ ] Implement Hilbert curve sorting
```

## Environment variables

`tasker` sets these for every goose invocation:

```bash
GOOSE_CONTEXT_STRATEGY=summarize
GOOSE_AUTO_COMPACT_THRESHOLD=0.35
```

## Session persistence

Each run generates unique session names for QA and Developer:

```
Developer session: dev_20260413_111500_a1b2c3
QA session:       qa_20260413_111500_d4e5f6
```

These are reused across all tasks within a run via `goose run --name`, giving the agents persistent context. The Developer agent accumulates knowledge across tasks; the QA agent builds a review history.

Sessions can be inspected with `goose session list`.

## JSONL log format

Each line is a JSON object:

```json
{
  "timestamp": "2026-04-13T11:15:00Z",
  "iteration": 1,
  "actor": "dev",
  "task_label": "P1.T1",
  "status": "in_progress",
  "payload": {
    "status": "done",
    "summary": "Created workspace Cargo.toml",
    "files_modified": ["Cargo.toml"]
  }
}
```

Status values: `assigned`, `in_progress`, `feedback`, `approved`, `error`, `blocked`, `needs_user_input`.

## Customizing recipes

Edit `recipes/recipe-dev.yaml` and `recipes/recipe-qa.yaml` to adjust agent behavior. The key requirements:

- **Developer** must return JSON with `"status"` (`"done"` or `"blocked"`), `"summary"`, `"files_modified"`, `"notes"`. When blocked, include `"blocker_description"` and `"blocker_suggestion"`.
- **QA** must return JSON with `"decision"` (`"approve"`, `"reject"`, or `"needs_user_input"`), `"feedback"`, `"concerns"`. When requesting user input, include `"user_question"`.

Recipe parameters are passed via `--params` and substituted into the `prompt:` template with `{{ param_name }}` Jinja-style syntax.

## Error handling summary

| Situation | What happens |
|-----------|-------------|
| **Dev returns `status: "blocked"`** | Blocker info sent to QA for triage. QA can guide dev, request user input, or approve. |
| **QA returns `needs_user_input`** | Interactive chat mode: user types answers, QA processes them until resolved. |
| **Dev returns unparseable output** | Graceful degradation: 1× normal → 3× continue → 3× subtask → 3× summarize → synthetic blocked → QA triage. |
| **QA returns unparseable output** | Treated as rejection with raw text as feedback. |
| **Dev subprocess crashes** | Logged as error, retried within current recovery stage. (Timeouts are handled separately — see below.) |
| **Max QA↔Dev iterations reached** | Task is skipped (not marked done — will retry on next run). |
| **Max chat turns reached** | Best-effort continue — pipeline resumes. |
| **`--jj` enabled but no jj repo** | JJ integration silently disabled with a warning. Tasks run normally without version control. |
| **`--jj` enabled but no base commit** | JJ integration silently disabled. A warning is printed suggesting `jj commit` first. |
| **Developer times out (default 10min)** | Process is killed. Dev returns a blocked response with timeout context. On next iteration, dev is relaunched with awareness it was stuck and told to finish quickly. |
| **QA times out (default 10min)** | Treated as rejection. Dev gets feedback explaining QA timed out, so dev can retry without waiting for a review. |
| **QA times out during chat** | Chat continues — the timeout is logged and QA response falls through to raw text display.

## Running tests

```bash
cd tools/tasker
uv run python tests/test_dryrun.py
```

## Project structure

```
tools/tasker/
├── pyproject.toml
├── README.md
├── recipes/
│   ├── recipe-dev.yaml          # Developer agent recipe
│   └── recipe-qa.yaml           # QA reviewer recipe (also handles chat mode)
├── src/tasker/
│   ├── __init__.py
│   ├── __main__.py              # python -m tasker
│   ├── main.py                  # Typer CLI entry point
│   ├── models.py                # Dataclasses (Task, Phase, payloads, RecoveryStage)
│   ├── parser.py                # Markdown task-list parser
│   ├── goose.py                 # Goose subprocess runner + JSON extraction
│   ├── orchestrator.py          # QA↔Dev loop + recovery + chat mode
│   ├── jj.py                    # Jujutsu (jj) VCS integration helpers
│   ├── log.py                   # JSONL iteration logger
│   └── ui.py                    # Rich progress bars + live table + chat input
├── docs/
│   └── jj-option-b.md           # Option B checkpointing workflow (future)
└── tests/
│   ├── fixtures/
│   │   ├── sample_tasks.md
│   │   └── e2e_test.md
│   └── test_dryrun.py
```
