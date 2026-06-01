# 07 — VCS Auto-Init for Multi-Repo Workspaces

## Problem

A tasker run may span multiple code directories (e.g. `eudox/`, `eudox-mcp/`,
`plans-mcp/`) under a single workspace root.  The orchestrator runs with
`--vcs git` against a single `cwd`, but only directories containing a `.git`
are tracked by the VCS backend.

When a task modifies files in a directory **without** `.git`:

```
1. _vcs_begin_task()   → creates feature branch on cwd repo  ✓
2. Dev agent works      → edits files in eudox-mcp/           ✓
3. _vcs_get_diff()      → diff against base commit            ✗
   Files in eudox-mcp/ are invisible to parent repo's git
   → diff is EMPTY
4. Empty diff → dev_response downgraded to "blocked"          ✗
5. Task loops forever — every real "done" is rejected
```

The **empty-diff downgrade** (INV-4) is a critical safety net that catches
false `done` claims.  But it **backfires** when the real work lives outside
the git-tracked tree.

---

## 7.1 Detection: task text references a non-git directory

The task text often contains directory hints:

```
- [ ] Implement MCP server in `eudox-mcp/src/eudox_mcp/server.py`
- [ ] Add auth middleware in `plans-mcp/src/plans_mcp/auth.py`
- [ ] Create `src/eudox/pipeline/analytics.py`
```

The orchestrator already knows its `cwd`.  Before starting a task, it can:

1. Extract directory references from the task text
2. Resolve them relative to `cwd`
3. Check if each resolved directory is inside a git working tree
4. If not, and the path is unambiguous → auto-init git there

---

## 7.2 Auto-init flow

### When it triggers

In `GitBackend.init()`, after the primary `cwd` repo is initialized:

```python
def init(self, cwd: Path | None = None) -> None:
    # ... existing init for cwd ...

def init_subdir(self, subdir: Path) -> None:
    """Auto-init git in a subdirectory if it's not already a repo."""
    check = _run_git(
        ["rev-parse", "--is-inside-work-tree"], cwd=subdir
    )
    if check.success:
        return  # already a git repo — nothing to do

    log.warning("vcs.auto_init", path=str(subdir))

    # 1. git init
    init = _run_git(["init"], cwd=subdir)
    if not init.success:
        raise RuntimeError(f"git init failed in {subdir}: {init.stderr}")

    # 2. Create .gitignore (Python defaults)
    gitignore = subdir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "__pycache__/\n.venv/\n*.pyc\n.ruff_cache/\n.pytest_cache/\n"
        )

    # 3. Initial commit — baseline snapshot
    _run_git(["add", "-A"], cwd=subdir)
    _run_git(
        ["commit", "-m", "tasker: baseline snapshot", "--allow-empty"],
        cwd=subdir,
    )

    log.info("vcs.auto_init_complete", path=str(subdir))
```

### Who calls it

The **orchestrator** scans task text for directory references before the
first task starts, not the GitBackend.  The orchestrator decides *which*
directories to init; the backend only does the `git init`.

```python
# In Orchestrator._scan_vcs_paths()
def _scan_vcs_paths(self) -> list[Path]:
    """Scan all task texts for directory references not tracked by git."""
    if self.vcs is None:
        return []

    candidates: set[Path] = set()
    for phase in self.phases:
        for task in phase.tasks:
            refs = _extract_dir_refs(task.text, self.cwd)
            candidates.update(refs)

    untracked = []
    for path in sorted(candidates):
        check = _run_git(
            ["rev-parse", "--is-inside-work-tree"],
            cwd=str(path),
        )
        if not check.success:
            untracked.append(path)

    return untracked
```

### Directory reference extraction

```python
import re

# Matches: `path/to/dir/` or `path/to/dir/file.py` in backticks
_DIR_REF_RE = re.compile(r"`([^`\s]+/(?:src|tests|crates|modules|pkg)/[^`\s]*)`")

def _extract_dir_refs(text: str, cwd: Path) -> set[Path]:
    """Extract directory references from task text.

    Looks for paths in backticks that contain known source directories
    (src/, tests/, crates/, modules/, pkg/).  Returns the set of
    unique root directories (the top-level dir containing src/ or tests/).

    For example:
      `eudox-mcp/src/eudox_mcp/server.py` → {cwd / "eudox-mcp"}
      `plans-mcp/tests/test_auth.py`       → {cwd / "plans-mcp"}
      `src/eudox/pipeline/analytics.py`    → {} (inside cwd repo, no sub-dir)
    """
    roots: set[Path] = set()
    for m in _DIR_REF_RE.finditer(text):
        raw_path = m.group(1)
        parts = Path(raw_path).parts
        # Find the boundary: the dir that directly contains src/ or tests/
        for i, part in enumerate(parts):
            if part in ("src", "tests", "crates", "modules", "pkg") and i > 0:
                root = cwd / Path(*parts[:i])
                if root.is_dir():
                    roots.add(root)
                break
    return roots
```

### When extraction is ambiguous

If no backtick-wrapped paths are found in the task text, the orchestrator
cannot determine which directory to init.  In this case:

1. Log `vcs.no_dir_refs_found` with the task label
2. Proceed normally — VCS will work for tracked files, empty-diff for untracked
3. The task may hit empty-diff downgrade, which is **safe** (blocks, not approves)

The orchestrator **never asks the user interactively** during a run — that
would block the automated loop.  If the user needs to init a directory,
they should do it before starting the run or add a setup task to the todo
that does `git init`.

---

## 7.3 Orchestrator integration

The auto-init scan happens **once at startup**, after parsing tasks but before
the first task begins:

```python
# In Orchestrator.run(), after self.phases = load_tasks(...)

if self.vcs is not None:
    untracked = self._scan_vcs_paths()
    for path in untracked:
        self.ui.print_info(f"VCS: auto-init git in {path.relative_to(self.cwd)}")
        try:
            self.vcs.init_subdir(path)
        except RuntimeError as exc:
            self.ui.print_warning(
                f"VCS: failed to auto-init {path}: {exc}. "
                f"Files in this directory will not be VCS-tracked."
            )
```

---

## 7.4 Safety guarantees

| Concern | Mitigation |
|---|---|
| Accidentally init git in `.venv/` or `__pycache__/` | Extraction only matches paths containing `src/`, `tests/`, `crates/`, `modules/`, or `pkg/` |
| Overwriting existing `.gitignore` | Only writes `.gitignore` if it doesn't already exist |
| Breaking existing repos | `_run_git(["rev-parse", "--is-inside-work-tree"])` check skips repos that already have git |
| Committing secrets | Auto-init only adds files that are already on disk. Standard `.gitignore` excludes `.env`, `__pycache__/`, etc. |
| User doesn't want auto-init | User can run with `--vcs none` to disable all VCS, or pre-init the directories themselves |

---

## 7.5 What about VCS diff for auto-init'd repos?

The tasker's `GitBackend` operates on a **single `cwd`**.  Auto-init'd
subdirectories are separate git repos — the parent repo doesn't track them.

For the empty-diff-downgrade check (INV-4), the orchestrator calls
`self.vcs.get_diff(task, cwd=self.cwd)`.  This only sees changes in the
`cwd` repo.

**For now, this is acceptable.** The auto-init ensures:

1. **Files are tracked** — `git init` + baseline commit means files are
   now in a repo.  The developer agent can run `git diff` inside that
   directory to see its own changes.
2. **QA gets the task text** — the diff isn't injected into QA context
   for sub-repo files, but the QA agent can read the files directly.
3. **No false `done` → `blocked` downgrade** — the empty-diff check only
   fires when `self.vcs is not None` AND the cwd repo's diff is empty.
   If the dev changed files in a sub-repo, the cwd repo's diff IS empty,
   so the downgrade still fires.  **This remains an open issue.**

### Future improvement: multi-repo diff

A future spec can add `_vcs_get_diff_multi()` that:

1. Detects which sub-repos the task touched (from `files_modified` in DevResponse)
2. Calls `get_diff()` on each sub-repo
3. Concatenates diffs for QA context
4. Uses the concatenated diff for empty-diff check

This is out of scope for this spec.  The auto-init is the prerequisite —
you can't diff what isn't tracked.

---

## 7.6 CLI interface

No new CLI flags.  The behavior is:

- **`--vcs git`** (existing): auto-init subdirectories detected from task text
- **`--vcs none`** (existing): no VCS, no auto-init
- **`--vcs jj`** (existing): no auto-init (jj workspaces have different structure)

The auto-init is transparent — it happens automatically when the conditions
are met and silently skips directories that are already repos.
