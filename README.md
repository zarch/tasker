# tasker

Goose-based task orchestration CLI with a QA/Dev feedback loop, interactive issue resolution, graceful error recovery, and optional version control integration (Jujutsu or Git).

`tasker` reads a markdown task list, assigns each task to a **Developer** goose agent, then sends the result to a **QA Reviewer** goose agent. If QA rejects the work, feedback is routed back to the developer in a loop until the task is approved — then the next task begins. When agents encounter blockers or return malformed output, `tasker` escalates gracefully through multiple recovery strategies, including an interactive chat mode where the user can resolve ambiguities directly.

## Screenshot

![tasker interface](docs/screen.png)

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
              --vcs jj

# With Git integration — each task gets a squash-merged commit on a feature branch
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --vcs git

# Rotate sessions per phase (## heading) instead of default sub-phase
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --session-scope phase

# Rotate sessions per task (fresh context every task)
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --session-scope task

# Force a new session on the next task (one-shot)
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --new-session
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
| `--vcs` | `none` | VCS integration: `jj` (Jujutsu), `git` (feature branch + squash merge), or `none` (disabled) |
| `--session-scope` | `subphase` | When to rotate goose sessions: `phase` (per `##`), `subphase` (per `###`), or `task` (per `- [ ]`) |
| `--new-session` | *(off)* | Force a new goose session on the next task (one-shot) |
| `--monitor-log` | `tasker.log` | Structured monitor log path (set to empty or use `--no-monitor-log` to disable) |
| `--no-monitor-log` | *(off)* | Disable the monitor log file (console/stderr logging still active) |
| `--log-level` | `WARNING` | Minimum level for console (stderr) output: `debug`, `info`, `warning`, `error`, `critical` |
| `--file-log-level` | `DEBUG` | Minimum level for the monitor log file: `debug`, `info`, `warning`, `error`, `critical` |
| `--no-rate-limit` | *(off)* | Disable automatic exponential backoff on transient connection/rate-limit errors |
| `--rate-limit-base-delay` | `30` | Base delay (seconds) for exponential backoff on connection errors |
| `--rate-limit-max-delay` | `300` | Maximum backoff delay (seconds) |
| `--rate-limit-max-retries` | `5` | Max retries on transient errors before trying the fallback model |
| `--max-consecutive-empty` | `3` | Max consecutive empty-output goose calls before a task is marked permanently failed `[~]` |
| `--fallback-dev-provider` | *(auto)* | Fallback provider for the DEV role (see [Fallback models](#fallback-models)); needs `--fallback-dev-model` |
| `--fallback-dev-model` | *(auto)* | Fallback model for the DEV role; needs `--fallback-dev-provider` |
| `--fallback-qa-provider` | *(auto)* | Fallback provider for the QA role; needs `--fallback-qa-model` |
| `--fallback-qa-model` | *(auto)* | Fallback model for the QA role; needs `--fallback-qa-provider` |
| `--no-auto-fallback` | *(off)* | Disable the automatic `claude-code` fallback (alias: `--no-anthropic-fallback`) |
| `--auto-fallback-model` | `sonnet` | Model for the automatic `claude-code` fallback (alias: `--anthropic-fallback-model`) |
| `--escalate` | *(off)* | Escalate stuck tasks to a stronger model (see [Stuck-task escalation](#stuck-task-escalation)) |
| `--escalate-provider` | `claude-code` | Provider for escalation (implies `--escalate`) |
| `--escalate-model` | `sonnet` | Model for escalation, e.g. `opus` (implies `--escalate`) |
| `--escalate-roles` | `arch,dev` | Roles that run escalated: subset of `arch,dev,qa` |
| `--escalate-timeout` | `1800` | Timeout (seconds) for each escalated goose call; replaces `--timeout` for those calls |
| `--fallback-timeout` | *(= `--timeout`)* | Timeout (seconds) for each fallback goose call |

## Fallback models

When the primary model/provider hits transient errors (rate limits, connection drops, silent crashes) tasker retries with exponential backoff; after the retries are exhausted — or right away when the call **timed out** — it switches to a **fallback model** so the pipeline keeps making progress. The fallback runs in its own goose session (`<session>_fallback`), with `--fallback-timeout` if given (otherwise `--timeout`), and only for that call — the next orchestrator turn goes back to the primary. With a timeout-triggered fallback a single call can take up to `--timeout` + `--fallback-timeout`.

Fallbacks are resolved per role (DEV and QA can fall back to different models) with the following precedence:

1. **Explicit CLI flags** — `--fallback-{role}-provider` together with `--fallback-{role}-model`. Giving only one of the two is an error.
2. **Automatic claude-code fallback** — if the `claude` CLI is on `PATH`, goose's `claude-code` provider (model `sonnet` by default) is configured automatically. It runs on the Claude subscription, no API key needed. Override the model with `--auto-fallback-model`.
3. **None** — no fallback (the pipeline returns the failure to the orchestrator's recovery logic).

The automatic fallback is **skipped** when the primary provider is itself `claude-code`, and can be disabled with `--no-auto-fallback`.

```bash
# Automatic: the claude CLI is installed
export GOOSE_MODE=auto   # required by claude-code, see below
uv run tasker --dev recipes/recipe-dev.yaml --qa recipes/recipe-qa.yaml tasks.md

# Explicit fallback (overrides the automatic default)
uv run tasker ... --fallback-dev-provider ollama --fallback-dev-model qwen3.5:9b \
                  --fallback-qa-provider claude-code --fallback-qa-model sonnet

# Disable the automatic fallback
uv run tasker ... --no-auto-fallback
```

### The claude-code provider

goose's `claude-code` provider drives the local `claude` CLI as a subprocess: goose sends its system prompt and the recipe, Claude Code does the work with **its own** tools and its own `~/.claude` configuration (settings, `CLAUDE.md`, MCP servers). Consequences:

- **`GOOSE_MODE=auto` is required.** With `approve` / `smart_approve` goose rejects every tool call of this headless provider (`Tool approval required in non-interactive mode`). tasker does not change the approval mode on its own — export `GOOSE_MODE=auto` or set it in the goose config; tasker prints a warning at start-up when it is missing.
- **Results arrive through a side channel.** Claude Code's tool calls never appear in the goose envelope, so the `tasker.respond` output cannot be found in tool responses. `tasker.respond` therefore also writes its JSON to the per-call file named by `TASKER_RESPONSE_FILE`, which tasker reads as the last fallback.
- **Turn counts are not comparable.** One goose turn can contain a whole Claude Code session, so `--max-turns` and the turn counters say little; `--timeout` is the effective limit.

## Stuck-task escalation

With `--escalate` (or `--escalate-provider` / `--escalate-model`), a task that the stuckness check flags (repeated recovery exhaustions or too many iterations without QA approval) switches to the escalation model: from that point every call of an escalated role for **that task** uses it, until the orchestrator moves on to another task. The first switch starts fresh DEV/QA goose sessions, because a session is not resumed across providers. The Architect only runs for stuck tasks, so it always uses the escalation model when `arch` is in `--escalate-roles`. Escalated calls use `--escalate-timeout` (default 1800 s) instead of `--timeout`: stronger models are slower and the escalated tasks are the hard ones.

```bash
# Stuck tasks: Architect + Developer on Claude Opus via the subscription
export GOOSE_MODE=auto
uv run tasker ... --escalate --escalate-model opus

# Give up on a hung primary sooner, give the stronger models more time
uv run tasker ... --timeout 1200 --fallback-timeout 1800 \
                  --escalate --escalate-model opus --escalate-timeout 2400
```

## Version control integration

`tasker` supports optional VCS integration via the `--vcs` flag. When enabled, each approved task produces one clean commit, and the diff is injected into the QA prompt as `project_context` so QA sees exactly what changed.

Two backends are available:

| Backend | Flag | How it works |
|---------|------|-------------|
| **Jujutsu** | `--vcs jj` | Each task gets an isolated `jj new` change, committed on approval. Linear history, one commit per task. |
| **Git** | `--vcs git` | Each task gets a feature branch. On approval, the branch is squash-merged onto the base branch as a single commit. |
| None | `--vcs none` (default) | No version control — tasks are marked `[x]` in the markdown file only. |

### Jujutsu (jj) backend

When `--vcs jj` is enabled, the project directory must already be a jj repository (initialized with `jj git init`).

```
base ──► jj new "P1.T1: <task>" ──► dev implements ──► QA reviews diff ──► jj commit ──► next task
```

1. **Task begin**: `jj new <parent_change> -m "P1.T1: <task text>"` creates an isolated working-copy change.
2. **Developer** implements the task (no version control commands needed — jj tracks everything automatically).
3. **QA review**: `jj diff --from <base_change>` is computed and injected into the QA prompt as `project_context`.
4. **Task approved**: The orchestrator marks the task `[x]` in the markdown file, then `jj commit` finalizes the change into a single clean commit.
5. **Task rejected**: The working-copy change is reused — the developer's next iteration builds on the same change. On approval, only the final state is committed.
6. **Next task**: `jj new <committed_change>` chains from the previous task's commit.

**Requirements**: `jj` on `PATH`, a jj repository, at least one base commit.

### Git backend

When `--vcs git` is enabled, the working directory must be a clean git repository on a branch.

```
base ──► git checkout -b task/P1.T1 ──► dev implements ──► QA reviews diff ──► squash merge ──► next task
```

1. **Task begin**: `git checkout -b task/<label>` creates a feature branch from the current HEAD.
2. **Developer** implements the task (commits freely on the feature branch).
3. **QA review**: `git diff <base_commit>..<feature_branch>` is computed and injected into the QA prompt as `project_context`.
4. **Task approved**: The orchestrator marks the task `[x]` in the markdown, then the feature branch is squash-merged onto the base branch as a single commit (`git merge --squash` + `git commit`). The feature branch is deleted.
5. **Task rejected**: The feature branch is reused — the developer's next iteration builds on the same branch. On approval, only the final state is squash-merged.
6. **Next task**: A new feature branch is created from the updated base branch HEAD.

**Requirements**: `git` on `PATH`, a git repository on a branch (not detached HEAD), clean working tree at startup.

> **Important**: All VCS commands use flags to avoid opening `$EDITOR`. No manual intervention is ever required.

## Task file formats

`tasker` supports two formats for defining tasks: **markdown** (human-friendly) and **JSONL** (machine-friendly). The orchestrator detects the format from the file extension (`.md` or `.jsonl`) and handles both transparently.

### Markdown format

Markdown files with `## Phase N` headings and `- [ ]` / `- [x]` checkboxes. Optional `###` sub-phase headings group tasks for session scope control:

```markdown
## Phase 1 — MVP

### P1-1 Setup
- [ ] Create project workspace with Cargo.toml
- [ ] Implement core geometry types

### P1-2 Features
- [ ] Add R-Tree spatial index
- [ ] Implement Hilbert curve sorting

## Phase 2 — Advanced

- [x] Set up CI pipeline
- [ ] Add logging support
```

The `###` headings are recognized by the parser and used by `--session-scope subphase` to rotate goose sessions at sub-phase boundaries. Files without `###` headings work unchanged.

### JSONL format

Each line in a `.tasks.jsonl` file is a JSON object describing one task. This is the recommended format when an LLM generates the task list — the schema is strict and `tasker prepare validate` catches structural errors before the orchestrator runs.

```json
{"phase": 1, "phase_title": "GeometryType Enum", "subphase": "P1 Tasks", "task_id": "T0.1", "text": "Add serde.workspace = true to hay-vector/Cargo.toml.", "ref": "00-spec.md#T0.1", "depends_on": [], "done": false}
```

| Field | Type | Description |
|-------|------|-------------|
| `phase` | int | 1-based, sequential across the file (1, 2, 3, …) |
| `phase_title` | str | Phase heading text |
| `subphase` | str | Sub-heading text |
| `task_id` | str | Globally unique identifier, e.g. `T0.1`, `T2A.3`, `TDoc.5` |
| `text` | str | Concise but complete task description — no hard length limit |
| `ref` | str | Spec anchor with full design details, e.g. `00-spec.md#T0.1` |
| `depends_on` | list\[str\] | Only for non-obvious dependencies: cross-phase, cross-subphase, or backward references. Use `[]` when the task simply follows the previous one. |
| `done` | bool | `false` for new tasks |

When the orchestrator reads a `.jsonl` file, it also keeps a companion `.md` file in sync — marking tasks done in both files.

## `tasker prepare` — generate and validate task lists

The `prepare` subcommand provides tools for creating and validating JSONL task files. This is especially useful when asking an LLM to generate a task list — the LLM produces JSONL, you validate it, then convert to markdown for review.

### Show the expected format

```bash
uv run tasker prepare example
```

Prints a valid example JSONL entry with all field rules, quality guidelines, and limits. Use this as the reference when instructing an LLM to generate a task list.

### Validate a JSONL file

```bash
uv run tasker prepare validate 99-todo.tasks.jsonl
```

Checks the file against the schema and prints a summary table:

```
                               ✓ Valid: 32 tasks
┏━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┓
┃ Phase ┃ Title                    ┃ Subphase  ┃ Tasks ┃
┡━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━┩
│ 1     │ GeometryType Enum        │ P1 Tasks  │     7 │
│ 2     │ PrimitiveGeometry, ...   │ P2A Tasks │     4 │
│ ...   │                          │           │       │
└───────┴──────────────────────────┴───────────┴───────┘
```

Options: `--max-tasks N` (default 10 per subphase), `--max-subphases N` (default 6 per phase).

### Convert JSONL to markdown

```bash
uv run tasker prepare to-md 99-todo.tasks.jsonl
# Or specify output path:
uv run tasker prepare to-md 99-todo.tasks.jsonl -o specs/99-todo.md
```

Generates a human-readable markdown file that the orchestrator can also read directly.

### Convert markdown to JSONL

```bash
uv run tasker prepare to-jsonl 99-todo.md
```

Extracts tasks from an existing markdown file into JSONL format. Useful for migrating from markdown to JSONL.

### Typical LLM workflow

```bash
# 1. Ask the LLM to generate the task list
#    "Run `tasker prepare example` to see the JSONL format, then create 99-todo.tasks.jsonl"

# 2. Validate the LLM's output
uv run tasker prepare validate 99-todo.tasks.jsonl

# 3. Convert to markdown for your review
uv run tasker prepare to-md 99-todo.tasks.jsonl

# 4. Run the orchestrator (it reads JSONL directly)
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              99-todo.tasks.jsonl
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

### Session scope (`--session-scope`)

By default, goose sessions accumulate context across all tasks in a run. For large task files (20+ tasks), this can cause the context window to fill up, leading to truncated responses. The `--session-scope` option controls when new sessions are created:

| Scope | Flag | Boundary | When sessions rotate |
|-------|------|----------|---------------------|
| `phase` | `--session-scope phase` | `##` heading | Once per phase — most context, risk of overflow |
| `subphase` (default) | `--session-scope subphase` | `###` heading | Once per sub-phase — good balance |
| `task` | `--session-scope task` | `- [ ]` item | Every task — no overflow, no cross-task context |

Example with sub-phase scope:

```
Developer session: dev_20260413_111500_a1b2c3   ← P1-1 tasks
QA session:       qa_20260413_111500_d4e5f6

[P1.T4] Session scope changed: P1::P1-1 Database → P1::P1-2 API — rotating sessions
Developer session: dev_20260413_120300_g7h8i9   ← P1-2 tasks (fresh context)
QA session:       qa_20260413_120300_j9k0l1
```

### Manual session reset (`--new-session`)

If the agent starts getting slow or confused mid-subphase, use `--new-session` to force a fresh session on the very next task:

```bash
uv run tasker --dev recipes/recipe-dev.yaml \
              --qa recipes/recipe-qa.yaml \
              specs/arch/99-todo.md \
              --new-session
```

This is a one-shot flag — it fires once and then the normal scope-based rotation takes over.

## Structured monitoring

`tasker` uses [structlog](https://www.structlog.org/) for structured, key-value logging of all orchestration events. Logs are written to **two independent destinations**:

| Destination | Default level | Purpose |
|---|---|---|
| **Monitor log file** (`--monitor-log`) | `DEBUG` | Captures everything — orchestration decisions, session rotations, recovery escalations, subprocess launches, VCS ops, parser events, UI lifecycle. |
| **Console (stderr)** | `WARNING` | Shows warnings and errors in the terminal. Deliberately writes to **stderr** (not stdout) so it never interferes with Rich's Live UI, which renders on stdout. |

> **Why stderr?** Rich's `Live` display takes over stdout for the progress table and status bars. All structured log output goes to stderr so the two never mix — you can pipe stdout without capturing logs, and logs never corrupt the UI.

### Log files

- **`tasker.log`** (monitor log) — human-readable, key-value format. One entry per line with timestamp, level, event name, and context fields.
- **`<task_file>.iterations.jsonl`** (iteration log) — machine-parseable JSON records of QA↔Dev exchanges only. Controlled separately via `--log`.

The monitor log uses a `RotatingFileHandler` (10 MB max, 3 backups) so it won't grow unbounded on long runs.

### Controlling log levels

Console and file levels are independent. Examples:

```bash
# Defaults: warnings+ on console, everything in file
uv run tasker --dev ... --qa ... specs/arch/99-todo.md

# Verbose console — see all info+ in the terminal
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --log-level info

# Quiet file — only warnings+ written to disk
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --file-log-level warning

# Debug everything everywhere
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --log-level debug

# Disable file logging entirely (console/stderr only)
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --no-monitor-log

# Custom log file location
uv run tasker --dev ... --qa ... specs/arch/99-todo.md --monitor-log /tmp/debug.log
```

Accepted level values (case-insensitive): `debug`, `info`, `warning` (or `warn`), `error`, `critical` (or `crit`).

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
| **Dev returns unparsable output** | Graceful degradation: 1× normal → 3× continue → 3× subtask → 3× summarize → synthetic blocked → QA triage. |
| **QA returns unparsable output** | Treated as rejection with raw text as feedback. |
| **Dev subprocess crashes** | Logged as error, retried within current recovery stage. (Timeouts are handled separately — see below.) |
| **Max QA↔Dev iterations reached** | Task is skipped (not marked done — will retry on next run). |
| **Max chat turns reached** | Best-effort continue — pipeline resumes. |
| **VCS enabled but tool not found** | VCS silently disabled with a warning. Tasks run normally without version control. |
| **VCS init fails (no repo, detached HEAD, dirty tree)** | VCS silently disabled with a warning. Tasks run normally without version control. |
| **Git feature branch has no changes** | Squash merge skipped — empty commit is avoided. Branch is cleaned up. |
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
│   ├── taskfile.py              # JSONL task schema, validation, read/write
│   ├── prepare.py               # `tasker prepare` subcommands (validate, to-md, to-jsonl, example)
│   ├── adapter.py               # Unified MD/JSONL dispatch for orchestrator
│   ├── goose.py                 # Goose subprocess runner + JSON extraction
│   ├── orchestrator.py          # QA↔Dev loop + recovery + chat mode + VCS integration
│   ├── jj.py                    # Backward-compat re-exports (see vcs/jj_backend.py)
│   ├── log.py                   # JSONL iteration logger
│   ├── ui.py                    # Rich progress bars + live table + chat input
│   └── vcs/
│       ├── __init__.py          # VCSBackend protocol + create_backend() factory
│       ├── jj_backend.py        # Jujutsu VCS backend (jj new/commit/diff)
│       └── git_backend.py       # Git VCS backend (feature branch + squash merge)
├── docs/
│   └── jj-option-b.md           # Option B checkpointing workflow (future)
└── tests/
│   ├── fixtures/
│   │   ├── sample_tasks.md
│   │   ├── e2e_test.md
│   │   └── subphase_tasks.md
│   └── test_dryrun.py
```
