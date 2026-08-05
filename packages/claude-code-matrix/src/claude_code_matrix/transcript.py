"""Parse Claude Code JSONL transcripts into user/assistant message pairs."""

import json
import os
from pathlib import Path


def claude_projects_dir() -> Path:
    """Return the directory Claude Code writes session transcripts into.

    Honours CLAUDE_CONFIG_DIR, which relocates the whole ~/.claude tree.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(config_dir) if config_dir else Path.home() / ".claude"
    return root / "projects"


def is_claude_code_payload(payload: dict) -> bool:
    """Return True when a hook payload was produced by Claude Code itself.

    hooks/hooks.json is not read by Claude Code alone. Codex loads the same
    file out of an installed plugin — that is deliberate for the shared
    SessionStart sentinel, but it also means Codex fires *these* handlers, with
    a payload whose `session_id` is a Codex thread id. Nothing downstream can
    tell the difference: the handlers would register that Codex thread in the
    Claude session map and open a second, Claude-avatar Matrix room for a
    session the Codex bridge is already mirroring properly. One unit of work,
    two rooms, two notifications, two spoken replies.

    So the ownership test is positive rather than a guess about who else might
    be calling: every Claude Code hook event carries a `transcript_path` under
    the Claude transcript root (verified across SessionStart, UserPromptSubmit,
    PreToolUse, Stop and SessionEnd). Codex's own `transcript_path` points at
    its rollout file under CODEX_HOME, so it can never satisfy this. The
    session-id fallback keeps a resumed session working if a future payload
    ever drops the field.
    """
    raw = payload.get("transcript_path") or ""
    if raw:
        root = claude_projects_dir()
        try:
            if Path(raw).resolve().is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            # Unresolvable path (broken symlink, permissions) — fall back to a
            # literal comparison rather than claiming the session.
            if str(raw).startswith(str(root)):
                return True

    session_id = payload.get("session_id") or ""
    return bool(session_id and find_transcript(session_id))


def find_transcript(session_id: str) -> Path | None:
    """Find the JSONL transcript file for a session ID."""
    projects_dir = claude_projects_dir()
    if not projects_dir.exists():
        return None

    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        transcript = project_dir / f"{session_id}.jsonl"
        if transcript.exists():
            return transcript

    return None


def extract_messages(transcript_path: Path) -> list[dict]:
    """Extract user and assistant text messages from a JSONL transcript.

    Returns a list of dicts with keys: role ("user" or "assistant"), text.
    Skips tool calls, tool results, thinking blocks, progress, and queue ops.
    """
    messages = []

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            obj_type = obj.get("type")
            message = obj.get("message", {})
            role = message.get("role")
            content = message.get("content")

            if obj_type == "user" and role == "user":
                # User messages: content is a string (direct input)
                # or a list with tool_result blocks (skip those)
                if isinstance(content, str) and content.strip():
                    messages.append({"role": "user", "text": content.strip()})
                elif isinstance(content, list):
                    # Check for text blocks that aren't tool results
                    for block in content:
                        if block.get("type") == "text" and block.get("text", "").strip():
                            messages.append({"role": "user", "text": block["text"].strip()})

            elif obj_type == "assistant" and role == "assistant":
                # Assistant messages: content is a list of blocks
                # Only extract text blocks, skip thinking/tool_use
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text" and block.get("text", "").strip():
                            messages.append({"role": "assistant", "text": block["text"].strip()})

    return messages
