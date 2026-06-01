# 04 — Recipe Changes

## Goal

Update the dev and QA recipe YAML files to teach the agents the
JSON-first pattern and (optionally) the Pydantic import path.

---

## 4.1 Dev recipe (`recipes/recipe-dev.yaml`)

### Current instructions (key excerpt)

```yaml
instructions: |
  ...
  ## JSON Response Format
  ### Success:
  {"status": "done", "summary": "...", "files_modified": [...], "notes": "..."}

  ### Blocked:
  {"status": "blocked", "summary": "...", "files_modified": [], ...}
```

### Proposed changes

Add a **JSON-First Rule** section at the top of instructions (before workflow):

```yaml
instructions: |
  ...
  ## ⚡ JSON-First Rule (MANDATORY)

  You MUST output a JSON block as your FIRST assistant message — before any
  tool calls.  This "checkpoint" ensures the orchestrator always gets a
  valid response even if you run out of tokens later.

  **Step 0 — Immediate checkpoint (before ANY tool calls):**
  ```json
  {"status": "blocked", "summary": "Starting: <task_label>", "files_modified": [], "notes": "checkpoint"}
  ```

  **After all work is done — update with final result:**
  ```json
  {"status": "done", "summary": "What was implemented", "files_modified": ["path/to/file"], "notes": ""}
  ```

  If the task is genuinely blocked, your final JSON keeps `status: "blocked"`.

  **Only the LAST JSON block in your response is parsed**, so your final
  update always overrides the checkpoint.

  ## Structured Output Helper (optional but recommended)

  A Pydantic model is available for guaranteed-valid JSON:

  ```python
  from tasker.schema import DevResponse
  resp = DevResponse(status="done", summary="...", files_modified=["file.rs"])
  print(resp.model_dump_json())
  ```

  Use this if you're uncertain about JSON syntax.  Raw JSON in a code block
  is also acceptable.
```

### Updated prompt section

```yaml
prompt: |
  ...
  **CRITICAL — JSON-first discipline:**
  1. **Turn 1**: Output checkpoint JSON immediately. NO tool calls before this.
  2. **Turns 2–3**: Quick code check (`rg` the target type/function).
  3. **If code exists** → turns 4–6: verify + output final JSON. STOP.
  4. **If code missing** → turns 4–16: read specs + implement.
  5. **Turn 17+**: Output final JSON NOW, even if work incomplete (status: "blocked").
  6. **Turn 35+**: ABSOLUTE deadline — JSON NOW.
```

The key change: **turn 1 is now a JSON output, not a tool call.**

---

## 4.2 QA recipe (`recipes/recipe-qa.yaml`)

Same pattern applied to QA:

```yaml
instructions: |
  ...
  ## ⚡ JSON-First Rule (MANDATORY)

  Output a checkpoint decision block as your FIRST assistant message:

  ```json
  {"decision": "reject", "feedback": "Review in progress", "concerns": ["checkpoint"]}
  ```

  After completing the review, output your final decision block.
  Only the LAST JSON block is parsed.

  ## Structured Output Helper (optional)

  ```python
  from tasker.schema import QAResponse
  resp = QAResponse(decision="approve", feedback="Looks good", concerns=[])
  print(resp.model_dump_json())
  ```
```

### Updated prompt section

```yaml
prompt: |
  ...
  **CRITICAL — JSON-first discipline:**
  1. **Turn 1**: Output checkpoint JSON. NO tool calls before this.
  2. **Turns 2–3**: Quick code check + read dev report.
  3. **If code exists and looks correct** → turns 4–6: verify + final JSON. STOP.
  4. **If concerns** → turns 4–12: read specs + inspect code + final JSON.
  5. **Turn 30+**: ABSOLUTE deadline — JSON NOW.
```

---

## 4.3 Why checkpoint is `reject` / `blocked`, not `approve`

If the agent dies after the checkpoint, the orchestrator sees:

- **Dev `blocked`**: QA triages the blocker → gives guidance → dev retries.  Productive.
- **QA `reject`**: Dev gets feedback ("review in progress") → re-implements → QA retries.  Productive.

If we used `approve` as the checkpoint:

- **Dev `done`**: QA reviews, likely approves since dev "said done" → task marked complete
  **without actual work being done**.  Dangerous.
- **QA `approve`**: Task finalized immediately, even if the real review never happened.  Dangerous.

**Conservative defaults prevent false completions.**

---

## 4.4 What about the decompose and arch recipes?

`recipe-qa-decompose.yaml` and `recipe-arch.yaml` also produce JSON output.
The same JSON-first pattern applies.  Changes are analogous:

- Decompose: checkpoint `{"should_decompose": false, "reason": "analyzing..."}`,
  then final response
- Arch: checkpoint `{"action": "retry", "reason": "analyzing..."}`, then final

These are lower priority since decompose/arch run once per task (not in a
tight loop) and their failure rate is much lower.
