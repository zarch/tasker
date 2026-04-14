"""Goose subprocess runner — wraps `goose run` with JSON output parsing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GooseRunResult:
    """Result of a goose run invocation."""

    success: bool
    raw_stdout: str
    raw_stderr: str
    return_code: int
    parsed_json: dict | None = None
    duration_secs: float = 0.0
    timed_out: bool = False


def _extract_json_block(text: str) -> dict | None:
    """Try to extract a JSON object from text that may contain markdown fences."""
    # Try: raw JSON first
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Try: fenced code block  ```json ... ```
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    # Try: last { ... } block
    last_brace = text.rfind("{")
    if last_brace != -1:
        try:
            obj = json.loads(text[last_brace:])
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass

    return None


def _extract_last_assistant_text(raw_stdout: str) -> str:
    """Extract the last assistant message text from goose JSON output.

    goose run --output-format json returns: {"messages": [...]}
    Each message has {"role": "user|assistant", "content": [{"type": "text", "text": "..."}]}
    We concatenate all assistant texts and return them for JSON extraction.

    Returns empty string when the envelope is valid but contains no assistant
    messages (e.g. goose errored before the agent responded), so that
    _extract_json_block correctly returns None instead of parsing the envelope
    itself as a structured response.
    """
    try:
        envelope = json.loads(raw_stdout)
        if isinstance(envelope, dict) and "messages" in envelope:
            messages = envelope["messages"]
            # Collect all assistant message texts
            assistant_texts = []
            for msg in messages:
                if msg.get("role") == "assistant":
                    for content in msg.get("content", []):
                        if content.get("type") == "text" and content.get("text"):
                            assistant_texts.append(content["text"])
            if assistant_texts:
                return "\n\n".join(assistant_texts)
            # Valid envelope but no assistant messages — return empty so
            # _extract_json_block returns None (not the envelope itself)
            return ""
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return raw_stdout


def build_goose_command(
    recipe_path: str | Path,
    session_name: str,
    params: dict[str, str] | None = None,
    max_turns: int = 80,
    model: str | None = None,
    provider: str | None = None,
) -> list[str]:
    """Build the goose CLI command list.

    Uses `--name` for session persistence (auto-resumes existing sessions).
    Uses `--params KEY=VALUE` to pass data into recipe templates.
    `--text` and `--recipe` are mutually exclusive, so we use --params.
    """
    cmd = [
        "goose",
        "run",
        "--recipe",
        str(recipe_path),
        "--name",
        session_name,
        "--output-format",
        "json",
        "--quiet",
        "--max-turns",
        str(max_turns),
        "--with-builtin",
        "developer",
    ]
    if params:
        for key, value in params.items():
            # Escape newlines and other shell-unsafe characters in values
            safe_value = (
                value.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            )
            cmd.extend(["--params", f"{key}={safe_value}"])
    if model:
        cmd.extend(["--model", model])
    if provider:
        cmd.extend(["--provider", provider])
    return cmd


def run_goose(
    recipe_path: str | Path,
    session_name: str,
    params: dict[str, str] | None = None,
    max_turns: int = 80,
    timeout_secs: int = 600,
    model: str | None = None,
    provider: str | None = None,
    cwd: str | Path | None = None,
) -> GooseRunResult:
    """Run goose synchronously and return parsed result.

    Uses Popen + communicate(timeout) so we can explicitly kill the
    goose process (and its children) when the timeout expires, rather
    than relying on subprocess.run which may leave orphans.

    Returns a GooseRunResult with timed_out=True when the process is killed.
    """
    cmd = build_goose_command(
        recipe_path=recipe_path,
        session_name=session_name,
        params=params,
        max_turns=max_turns,
        model=model,
        provider=provider,
    )

    # Merge required env vars with the current process environment
    env = os.environ.copy()
    env["GOOSE_CONTEXT_STRATEGY"] = "summarize"
    env["GOOSE_AUTO_COMPACT_THRESHOLD"] = "0.35"

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd) if cwd else None,
            # Start a new process group so we can kill the whole tree
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout_secs)
            duration = time.monotonic() - start
            stdout = stdout.strip()
            stderr = stderr.strip()

            # Extract assistant text from the goose JSON envelope,
            # then try to parse a structured JSON response from it.
            assistant_text = _extract_last_assistant_text(stdout)
            parsed = _extract_json_block(assistant_text)

            return GooseRunResult(
                success=proc.returncode == 0,
                raw_stdout=assistant_text,
                raw_stderr=stderr,
                return_code=proc.returncode,
                parsed_json=parsed,
                duration_secs=round(duration, 2),
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            # Kill the entire process group (goose + any child processes)
            duration = time.monotonic() - start
            try:
                os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
            except (ProcessLookupError, OSError):
                proc.kill()
            proc.communicate()  # reap to avoid zombies
            timeout_minutes = timeout_secs / 60
            return GooseRunResult(
                success=False,
                raw_stdout="",
                raw_stderr=(
                    f"TIMEOUT after {timeout_secs}s ({timeout_minutes:.0f}min) — "
                    f"goose process killed"
                ),
                return_code=-1,
                duration_secs=round(duration, 2),
                timed_out=True,
            )
    except Exception as exc:
        duration = time.monotonic() - start
        return GooseRunResult(
            success=False,
            raw_stdout="",
            raw_stderr=f"Failed to start goose process: {exc}",
            return_code=-1,
            duration_secs=round(duration, 2),
            timed_out=False,
        )
