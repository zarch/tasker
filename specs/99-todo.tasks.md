# Implementation TODO


## Phase 1 — Pydantic Schema & Dependency


### P1 Schema Module

- [x] **P1.1** Add `pydantic>=2.0` to the `dependencies` list in `pyproject.toml`. Pydantic is currently available transitively but not declared — this makes it a direct dependency so the agent import path is guaranteed. (Ref: 02-pydantic-models.md#2.1)
- [x] **P1.2** Create `src/tasker/schema.py` with Pydantic v2 BaseModel classes: `DevResponse(status: Literal["done","blocked"], summary: str, files_modified: list[str], notes: str, blocker_description: str, blocker_suggestion: str)`, `QAResponse(decision: Literal["approve","reject","needs_user_input"], feedback: str, concerns: list[str], user_question: str)`, `DecomposeResponse(should_decompose: bool, reason: str, subtasks: list[SubtaskSchema])`, `ArchResponse(action: Literal["recompose","clarify","skip","retry"], reason: str, subtasks: list[SubtaskSchema], new_task_text: str, max_iterations_override: int|None)`, and a helper `SubtaskSchema(label: str, text: str)`. All fields must have Field() descriptions. The module must have zero imports from other tasker modules — it is the public surface exposed to agents. (Ref: 02-pydantic-models.md#2.1)
- [x] **P1.3** Add unit tests in `tests/test_dryrun.py` for `schema.py`: (1) `test_schema_dev_response_valid_done` — construct DevResponse with status="done", verify `model_dump_json()` produces parseable JSON; (2) `test_schema_dev_response_valid_blocked` — same with status="blocked"; (3) `test_schema_dev_response_invalid_status` — verify `status="unknown"` raises `ValidationError`; (4) `test_schema_qa_response_all_decisions` — verify all three literal values accepted; (5) `test_schema_qa_response_invalid_decision` — verify invalid raises ValidationError; (6) `test_schema_decompose_response` — verify roundtrip; (7) `test_schema_arch_response_all_actions` — verify all four literal values. (Ref: 02-pydantic-models.md#2.1)

## Phase 2 — Cascade JSON Extraction


### P2 Extract Function

- [x] **P2.1** In `src/tasker/goose.py`, add a new function `_extract_json_blocks(text: str) -> list[dict]` that extracts ALL valid JSON dicts from text in document order. Scan for: (1) all fenced ```json...``` code blocks via `re.finditer`, (2) all bare `{...}` brace pairs. For each candidate, try `json.loads()` and collect valid dicts. Deduplicate: if two candidates parse to the same dict at overlapping positions, keep only the earlier one. Return the list ordered by first character position in the text. This function coexists with the existing `_extract_json_block` — do NOT remove the old one yet. (Ref: 06-open-questions.md#Q4)
- [x] **P2.2** Add `GooseRunResult.json_blocks_found: int = 0` and `GooseRunResult.json_blocks_cascade: bool = False` fields to the dataclass in `src/tasker/goose.py`. In `run_goose()`, after extracting `assistant_text`, call `_extract_json_blocks(assistant_text)` and set `json_blocks_found = len(blocks)`. If blocks is non-empty, set `parsed_json = blocks[-1]` (last valid block wins). Set `json_blocks_cascade = True` when the last brace/block in the text was malformed (i.e. there exists at least one malformed JSON candidate appearing after the last valid block). The old `_extract_json_block` call is replaced by the new cascade call. (Ref: 06-open-questions.md#Q4)

### P2 Tests

- [x] **P2.3** Add unit tests in `tests/test_dryrun.py` for `_extract_json_blocks`: (1) `test_extract_blocks_single_bare_json` — `'{"a":1}'` → one block; (2) `test_extract_blocks_fenced_json` — '```json\n{"a":1}\n```' → one block; (3) `test_extract_blocks_multiple_ordered` — two valid JSON dicts in order → list preserves order; (4) `test_extract_blocks_last_malformed_cascades` — valid block then truncated `{"a":` → returns only the valid block; (5) `test_extract_blocks_no_json` — plain text → empty list; (6) `test_extract_blocks_nested_braces` — `{"a":{"b":2}}` → single valid dict; (7) `test_extract_blocks_empty_string` → empty list; (8) `test_cascade_flag_set_in_goose_result` — mock a goose run where assistant_text has a valid block followed by a malformed one, verify `GooseRunResult.json_blocks_cascade == True` and `json_blocks_found == 1`. (Ref: 06-open-questions.md#Q4)

## Phase 3 — PYTHONPATH Injection


### P3 Env Setup

- [x] **P3.1** In `src/tasker/goose.py`, inside `run_goose()` where the env dict is built (after the cgroup block, before Popen), compute the tasker src directory: `tasker_src = str(Path(__file__).resolve().parent.parent)` (points to the `src/` containing `tasker/`). Prepend it to `PYTHONPATH`: `env["PYTHONPATH"] = tasker_src + os.pathsep + env.get("PYTHONPATH", "")`. This makes `from tasker.schema import DevResponse` available to the goose agent subprocess. Add a `log.debug("goose.pythonpath_injected", ...)` entry. (Ref: 02-pydantic-models.md#2.2)

### P3 Tests

- [x] **P3.2** Add unit test `test_pythonpath_injected_in_env` in `tests/test_dryrun.py`: mock subprocess.Popen, call `run_goose()` with a dummy recipe, capture the `env` passed to Popen, assert that `env["PYTHONPATH"]` contains the path to `src/` and that the original PYTHONPATH (if any) is preserved as a suffix. (Ref: 02-pydantic-models.md#2.2)

## Phase 4 — Orchestrator Pydantic Validation


### P4 Response Parsing

- [x] **P4.1** In `src/tasker/orchestrator.py`, update `_parse_dev_response(raw, parsed)` to use Pydantic validation as the primary path: wrap the existing logic in a `try: validated = DevResponseSchema.model_validate(parsed) ... except ValidationError: fall back to current ad-hoc dict extraction`. Import `from tasker.schema import DevResponse as DevResponseSchema`. The ad-hoc fallback preserves backward compat for edge cases Pydantic rejects (e.g., extra fields with wrong types). The function signature and return type `DevResponse | None` do not change. (Ref: 02-pydantic-models.md#2.3)
- [x] **P4.2** In `src/tasker/orchestrator.py`, update `_parse_qa_response(raw, parsed)` with the same Pydantic-first pattern: try `QAResponseSchema.model_validate(parsed)`, fall back to ad-hoc on `ValidationError`. Import `from tasker.schema import QAResponse as QAResponseSchema`. (Ref: 02-pydantic-models.md#2.3)
- [x] **P4.3** In `src/tasker/orchestrator.py`, update `_parse_decompose_response(raw, parsed)` with the same Pydantic-first pattern using `from tasker.schema import DecomposeResponse as DecomposeResponseSchema`. The ad-hoc fallback handles the `should_decompose` string→bool coercion that Pydantic would reject. (Ref: 02-pydantic-models.md#2.3)
- [x] **P4.4** In `src/tasker/orchestrator.py`, update `_parse_arch_response(raw, parsed)` with the same Pydantic-first pattern using `from tasker.schema import ArchResponse as ArchResponseSchema`. (Ref: 02-pydantic-models.md#2.3)

### P4 Tests

- [x] **P4.5** Add unit tests in `tests/test_dryrun.py`: (1) `test_parse_dev_response_pydantic_valid` — valid dict with all fields → DevResponse returned; (2) `test_parse_dev_response_pydantic_invalid_status_fallback` — dict with `status="unknown"` → returns None (both Pydantic and ad-hoc reject it); (3) `test_parse_dev_response_extra_fields` — valid dict plus extra unknown fields → DevResponse returned (Pydantic ignores extras); (4) `test_parse_qa_response_pydantic_valid` — valid QA dict → QAResponse; (5) `test_parse_qa_response_invalid_decision` → None; (6) `test_parse_decompose_response_pydantic_valid` — valid dict → DecomposeResponse; (7) `test_parse_arch_response_pydantic_valid` — valid dict → ArchResponse. (Ref: 02-pydantic-models.md#2.3)

## Phase 5 — Metrics & Logging


### P5 IterationEntry Fields

- [x] **P5.1** Add optional fields to the `IterationEntry` dataclass in `src/tasker/models.py`: `checkpoint: bool = False`, `json_blocks_found: int = 0`, `json_blocks_cascade: bool = False`, `assistant_turns: int = 0`, `total_turns: int = 0`, `output_chars: int = 0`. Update `to_dict()` to include these fields when they have non-default values (i.e. `if self.checkpoint: d["checkpoint"] = True`, same pattern as existing optional fields). (Ref: 06-open-questions.md#Q8)

### P5 Envelope Metrics

- [x] **P5.2** In `src/tasker/goose.py`, update `_extract_last_assistant_text` to also return envelope metrics AND detect stale-only output: change return type from `tuple[str, bool]` to `tuple[str, bool, int, int]` where the two new ints are `assistant_turns` (count of messages with role="assistant") and `total_turns` (len(messages)). Additionally, fix a safety gap: when the envelope has assistant messages but NONE appear after the last user message (meaning goose replayed old history but produced no new response), treat this the same as empty_output — return `("", True, 0, total)` so stale JSON from a previous call cannot leak into the current iteration. When new assistant messages DO exist after the last user message, concatenate only those new messages (not the old history). Update all callers in `goose.py` (`run_goose`) and in `orchestrator.py` (where `_extract_last_assistant_text` is imported) to unpack the 4-tuple. The new values feed into `GooseRunResult` and then into `IterationEntry`. (Ref: 06-open-questions.md#Q8)

### P5 GooseRunResult Fields

- [x] **P5.3** Add fields `assistant_turns: int = 0`, `total_turns: int = 0`, `output_chars: int = 0` to `GooseRunResult` in `src/tasker/goose.py`. In `run_goose()`, populate them from the 4-tuple return of `_extract_last_assistant_text` and from `len(assistant_text)`. These values flow to the orchestrator which copies them into IterationEntry. (Ref: 06-open-questions.md#Q8)

### P5 Orchestrator Metrics

- [x] **P5.4** In `src/tasker/orchestrator.py`, update every `IterationEntry(...)` construction site to pass the new GooseRunResult-derived fields: `json_blocks_found`, `json_blocks_cascade`, `assistant_turns`, `total_turns`, `output_chars`. Do NOT set `checkpoint` here — that is handled by P5.5 which adds `_is_checkpoint()` detection. There are ~20 IterationEntry() call sites — find them by grepping for `IterationEntry(` and pass the fields from the GooseRunResult. For error/timeout entries where no GooseRunResult exists, pass defaults (0, False). (Ref: 06-open-questions.md#Q8)

### P5 Checkpoint Detection

- [x] **P5.5** Add `_is_checkpoint(response: DevResponse|QAResponse) -> bool` in `src/tasker/orchestrator.py`. For DevResponse: `return response.status == "blocked" and "checkpoint" in response.notes`. For QAResponse: `return response.decision == "reject" and "checkpoint" in response.feedback`. Integrate into `_run_dev_with_recovery` and `_run_qa_with_recovery`: after successful `_parse_dev_response`/`_parse_qa_response`, call `_is_checkpoint`, and if True emit `log.info("dev.checkpoint", task_label=..., checkpoint_summary=...)` or `log.info("qa.checkpoint", ...)` and set the IterationEntry `checkpoint=True` field. (Ref: 03-json-first-protocol.md#3.4)
- [x] **P5.6** In `src/tasker/orchestrator.py`, wherever `result.json_blocks_cascade == True`, emit a `log.info("json.cascade", task_label=..., actor="dev"|"qa", blocks_found=result.json_blocks_found)` event. This goes in `_run_dev_with_recovery` and `_run_qa_with_recovery` after the goose call returns. (Ref: 06-open-questions.md#Q8)

### P5 Tests

- [x] **P5.7** Add unit tests in `tests/test_dryrun.py`: (1) `test_iteration_entry_new_fields_serialized` — construct IterationEntry with checkpoint=True, json_blocks_found=3, etc, call to_dict(), assert all fields present; (2) `test_iteration_entry_defaults_omitted` — construct with all defaults, to_dict() should NOT contain the new keys; (3) `test_extract_last_assistant_text_returns_metrics` — call with a multi-message envelope, verify assistant_turns and total_turns counts are correct; (4) `test_is_checkpoint_dev_blocked_with_checkpoint_notes` → True; (5) `test_is_checkpoint_dev_done` → False; (6) `test_is_checkpoint_qa_reject_with_checkpoint` → True; (7) `test_is_checkpoint_qa_approve` → False. (Ref: 06-open-questions.md#Q8)

## Phase 6 — Recipe JSON-First Rewrite


### P6 Dev Recipe

- [x] **P6.1** Rewrite the instructions and prompt sections of `recipes/recipe-dev.yaml`: (1) Add a `## ⚡ JSON-First Rule (MANDATORY)` section before the workflow section, stating the agent MUST output a checkpoint JSON block as its FIRST assistant message before any tool calls: `{"status": "blocked", "summary": "Starting: {{ task_label }}", "files_modified": [], "notes": "checkpoint"}`. (2) Add a `## Structured Output Helper` section showing the Pydantic import: `from tasker.schema import DevResponse; print(DevResponse(...).model_dump_json())`. (3) Replace the existing `JSON Response Format` section to show both checkpoint and final JSON examples. (4) Update the prompt's `CRITICAL — JSON-first discipline` section to: Turn 1 = checkpoint JSON (no tool calls), Turns 2-3 = quick code check, Turns 4-16 = implement, Turn 17+ = output final JSON, Turn 35+ = absolute deadline. Remove the old `CRITICAL — JSON block discipline` paragraph. (Ref: 04-recipe-changes.md#4.1)

### P6 QA Recipe

- [~] **P6.2** Rewrite the instructions and prompt sections of `recipes/recipe-qa.yaml`: (1) Add `## ⚡ JSON-First Rule (MANDATORY)` section requiring a checkpoint as first message: `{"decision": "reject", "feedback": "Review in progress for {{ task_label }}", "concerns": ["checkpoint"]}`. (2) Add `## Structured Output Helper` section with `from tasker.schema import QAResponse` import example. (3) Replace the existing `JSON Response Format` section with checkpoint + final examples. (4) Update prompt's critical discipline to: Turn 1 = checkpoint JSON, Turns 2-3 = quick check, Turns 4-6 = verify if clean, Turns 4-12 = inspect if concerns, Turn 30+ = absolute deadline. Remove old `CRITICAL — JSON decision block discipline` paragraph. (Ref: 04-recipe-changes.md#4.2)

### P6 Decompose Recipe

- [~] **P6.3** Update `recipes/recipe-qa-decompose.yaml`: add JSON-First Rule section requiring initial checkpoint `{"should_decompose": false, "reason": "Analyzing task: {{ task_label }}"}` as first message. Add Structured Output Helper with `from tasker.schema import DecomposeResponse` example. Keep existing workflow logic but ensure turn 1 is always the checkpoint. (Ref: 04-recipe-changes.md#4.4)

### P6 Arch Recipe

- [~] **P6.4** Update `recipes/recipe-arch.yaml`: add JSON-First Rule requiring checkpoint `{"action": "retry", "reason": "Analyzing stuck task: {{ task_label }}"}` as first message. Add Structured Output Helper with `from tasker.schema import ArchResponse` example. (Ref: 04-recipe-changes.md#4.4)

## Phase 7 — Session Resume Safety Tests


### P7 Resume Tests

- [~] **P7.1** Add `test_session_resume_ignores_old_checkpoint` in `tests/test_dryrun.py`: build a goose JSON envelope with an old assistant message containing `{"status":"blocked","summary":"old"}` followed by a user message (recovery instruction) but NO new assistant message. Call `_extract_last_assistant_text(envelope)` and assert `empty_flag is True` and `text == ""`. This verifies that when goose returns session history with old checkpoints but the current call produced no new output, the old JSON block is NOT extracted. (Ref: 06-open-questions.md#Q5)
- [~] **P7.2** Add `test_session_resume_new_checkpoint_wins` in `tests/test_dryrun.py`: build an envelope with old assistant `{"status":"blocked","summary":"old"}`, a user message, and a NEW assistant message `{"status":"done","summary":"new"}`. Call `_extract_last_assistant_text`, verify `empty_flag is False`, then call `_extract_json_blocks` on the text and verify the last block has `status == "done"` and `summary == "new"`. This verifies that new output supersedes old checkpoints via last-wins. (Ref: 06-open-questions.md#Q5)
- [~] **P7.3** Add `test_session_resume_cascade_from_history` in `tests/test_dryrun.py`: build an envelope with two assistant messages — first has `{"status":"blocked","summary":"checkpoint"}`, second has `{"status":"done","summary":"incomplete` (truncated). Call `_extract_json_blocks` on the concatenated text and verify it returns exactly one valid block (the checkpoint), not the malformed one. Verifies cascade works correctly across multi-message concatenated text. (Ref: 06-open-questions.md#Q5)

## Phase 8 — Cleanup & Dead Code Removal


### P8 Removal

- [~] **P8.1** Remove the old `_extract_json_block(text: str) -> dict | None` function from `src/tasker/goose.py`. All callers in `goose.py` and `orchestrator.py` now use `_extract_json_blocks` (plural) via `GooseRunResult.parsed_json`. Grep for all remaining references to `_extract_json_block` (singular) and verify none remain. Update any import statements in `orchestrator.py` that reference the old function. (Ref: 06-open-questions.md#Q4)

### P8 Verification

- [~] **P8.2** Run the full test suite (`uv run pytest tests/test_dryrun.py -v`) and verify all tests pass — both old tests (adapted if they referenced `_extract_json_block`) and new tests. Run `uv run ruff check src/` and `uv run ty check src/` to verify no lint or type errors. Fix any remaining references to the old API. (Ref: 05-control-safety.md#5.6)

## Phase 9 — VCS Auto-Init for Multi-Repo Workspaces


### P9 Directory Extraction

- [~] **P9.1** Add `_extract_dir_refs(text: str, cwd: Path) -> set[Path]` in `src/tasker/orchestrator.py`. The function uses `re.compile(r"`([^`\s]+/(?:src|tests|crates|modules|pkg)/[^`\s]*)`")` to find paths in backticks that contain known source directories. For each match, walk up to find the directory that directly contains `src/` or `tests/` (etc) — that's the project root. Return unique roots that exist as directories on disk. Paths with no prefix before `src/` (i.e. inside cwd itself) are excluded since they're already tracked by the cwd repo. (Ref: 07-vcs-auto-init.md#7.2)
- [~] **P9.2** Add unit tests for `_extract_dir_refs` in `tests/test_dryrun.py`: (1) `test_extract_dir_refs_eudox_mcp` — text contains `` `eudox-mcp/src/eudox_mcp/server.py` `` → returns `{cwd / "eudox-mcp"}`; (2) `test_extract_dir_refs_plans_mcp` — text contains `` `plans-mcp/tests/test_auth.py` `` → returns `{cwd / "plans-mcp"}`; (3) `test_extract_dir_refs_inside_cwd` — text contains `` `src/eudox/pipeline/analytics.py` `` → returns empty set (no prefix before src/); (4) `test_extract_dir_refs_multiple` — text has two backtick paths in different dirs → both roots returned; (5) `test_extract_dir_refs_no_paths` — plain text with no backtick paths → empty set; (6) `test_extract_dir_refs_nonexistent_dir` — path in backticks doesn't exist on disk → excluded from results. (Ref: 07-vcs-auto-init.md#7.2)

### P9 Git Auto-Init

- [~] **P9.3** Add `init_subdir(subdir: Path) -> None` method to `GitBackend` in `src/tasker/vcs/git_backend.py`. Logic: (1) run `git rev-parse --is-inside-work-tree` in subdir — if success, return immediately (already a repo); (2) run `git init` in subdir — raise RuntimeError on failure; (3) create `.gitignore` with Python defaults (`__pycache__/`, `.venv/`, `*.pyc`, `.ruff_cache/`, `.pytest_cache/`) only if it doesn't exist; (4) `git add -A` + `git commit -m "tasker: baseline snapshot" --allow-empty`; (5) emit structlog `vcs.auto_init` and `vcs.auto_init_complete` events. (Ref: 07-vcs-auto-init.md#7.2)
- [~] **P9.4** Add unit tests for `GitBackend.init_subdir` in `tests/test_dryrun.py`. Use `tmp_path` fixture to create a directory structure: (1) `test_init_subdir_creates_repo` — call `init_subdir(tmp_path / "mymod")`, verify `.git/` exists and baseline commit is present; (2) `test_init_subdir_skips_existing_repo` — init a dir, then call `init_subdir` again — no error, no duplicate commits; (3) `test_init_subdir_creates_gitignore` — dir without `.gitignore` → created with Python defaults; (4) `test_init_subdir_preserves_existing_gitignore` — dir with custom `.gitignore` → not overwritten. (Ref: 07-vcs-auto-init.md#7.2)

### P9 Orchestrator Integration

- [~] **P9.5** Add `_scan_vcs_paths(self) -> list[Path]` method to `Orchestrator` in `src/tasker/orchestrator.py`. Iterates all tasks in `self.phases`, calls `_extract_dir_refs(task.text, self.cwd)` on each, collects unique roots, filters to those that are NOT inside a git working tree (`git rev-parse --is-inside-work-tree` fails). Returns sorted list. Then in `Orchestrator.run()`, after `self.phases = load_tasks(...)` and after VCS init, call `untracked = self._scan_vcs_paths()` and for each path call `self.vcs.init_subdir(path)` with a try/except that logs warning and continues on failure. Print UI info for each auto-init'd directory. (Ref: 07-vcs-auto-init.md#7.3)
- [~] **P9.6** Add integration-style unit test `test_scan_vcs_paths_detects_untracked` in `tests/test_dryrun.py`: create a temp workspace with `cwd/` (git repo) and `cwd/submod/src/file.py` (no git). Build a Phase with a task whose text contains `` `submod/src/file.py` ``. Call `_scan_vcs_paths()` — assert it returns `[cwd / "submod"]`. Then call `init_subdir` and verify the subdir becomes a git repo. (Ref: 07-vcs-auto-init.md#7.3)

### P9 Verification

- [~] **P9.7** Run full test suite (`uv run pytest tests/test_dryrun.py -v`), ruff (`uv run ruff check src/`), and type check (`uv run ty check src/`). Fix any issues. Verify no regression in existing 87+ tests. (Ref: 07-vcs-auto-init.md#7.4)
