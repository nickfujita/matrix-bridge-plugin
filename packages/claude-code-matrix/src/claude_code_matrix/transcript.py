"""Parse Claude Code JSONL transcripts into user/assistant message pairs."""

import json
from pathlib import Path


def find_transcript(session_id: str) -> Path | None:
    """Find the JSONL transcript file for a session ID."""
    projects_dir = Path.home() / ".claude" / "projects"
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
