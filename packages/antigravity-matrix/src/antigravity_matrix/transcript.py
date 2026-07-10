"""Parse Google Antigravity CLI JSONL transcripts.

Antigravity hook payloads include a transcript path like:
  ~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript_full.jsonl

Relevant JSONL entries observed in CLI transcripts:
  - USER_EXPLICIT / USER_INPUT: user prompt, wrapped in <USER_REQUEST> tags
  - MODEL / PLANNER_RESPONSE: model narration or final assistant text
  - MODEL / <tool type>: completed tool output
  - SYSTEM / CHECKPOINT and CONVERSATION_HISTORY: internal context, skipped
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


FINAL_MESSAGE_TYPES = {"PLANNER_RESPONSE"}
SKIPPED_TOOL_TYPES = {"PLANNER_RESPONSE", "CONVERSATION_HISTORY", "CHECKPOINT", "GENERIC"}
HIDDEN_USER_MARKERS = (
    "PAPER_VOICE_DAILY_RUN_ID=",
)


def _load_lines(transcript_path: Path) -> list[dict[str, Any]]:
    if not transcript_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
    return entries


def _strip_user_request(content: str) -> str:
    """Extract the human prompt from Antigravity's XML-ish wrapper."""
    match = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", content, re.DOTALL)
    text = match.group(1) if match else content
    return text.strip()


def _is_hidden_user_text(text: str) -> bool:
    return any(marker in text for marker in HIDDEN_USER_MARKERS)


def _is_final_planner_response(obj: dict[str, Any]) -> bool:
    """Return True for assistant text that should be treated as user-facing.

    Planner responses that include tool calls are progress narration for an
    upcoming tool call. They are useful in the terminal, but they are too noisy
    for mobile notifications and TTS, so only planner responses without
    tool_calls are mirrored as assistant messages.
    """
    return (
        obj.get("source") == "MODEL"
        and obj.get("type") in FINAL_MESSAGE_TYPES
        and obj.get("status") == "DONE"
        and bool(str(obj.get("content") or "").strip())
        and not obj.get("tool_calls")
    )


def _format_tool_message(obj: dict[str, Any]) -> str | None:
    if obj.get("source") != "MODEL" or obj.get("status") != "DONE":
        return None

    tool_type = str(obj.get("type") or "")
    if not tool_type or tool_type in SKIPPED_TOOL_TYPES:
        return None

    label = tool_type.lower().replace("_", " ")
    content = str(obj.get("content") or "")

    # Try to make file and command tool messages more useful without dumping
    # the whole tool output into Matrix.
    file_match = re.search(r"File Path:\s*`?file://([^`\n]+)`?", content)
    if file_match:
        return f"● {label}(\"{Path(file_match.group(1)).name}\")"

    command_match = re.search(r"Command(?: Line)?:\s*`?([^`\n]+)`?", content)
    if command_match:
        command = command_match.group(1).strip()
        if len(command) > 120:
            command = command[:117] + "..."
        return f"● {label}(\"{command}\")"

    return f"● {label}"


def extract_messages(transcript_path: Path) -> list[dict[str, str]]:
    """Extract user, assistant, and tool messages from a transcript.

    Returns dictionaries with role and text keys. Assistant messages are limited
    to final planner responses, not every intermediate tool-call narration.
    """
    messages: list[dict[str, str]] = []

    for obj in _load_lines(transcript_path):
        source = obj.get("source")
        typ = obj.get("type")
        status = obj.get("status")
        content = obj.get("content")

        if status != "DONE":
            continue

        if source == "USER_EXPLICIT" and typ == "USER_INPUT" and isinstance(content, str):
            text = _strip_user_request(content)
            if text and not _is_hidden_user_text(text):
                messages.append({"role": "user", "text": text})
            continue

        if _is_final_planner_response(obj):
            messages.append({"role": "assistant", "text": str(content).strip()})
            continue

        tool_text = _format_tool_message(obj)
        if tool_text:
            messages.append({"role": "tool", "text": tool_text})

    return messages


def extract_latest_assistant(transcript_path: Path) -> str | None:
    """Return the latest final assistant text from the transcript."""
    latest: str | None = None
    for obj in _load_lines(transcript_path):
        if _is_final_planner_response(obj):
            latest = str(obj.get("content") or "").strip()
    return latest or None


def extract_messages_after_count(transcript_path: Path, count: int) -> tuple[list[dict[str, str]], int]:
    """Return messages after a previously synced message count."""
    messages = extract_messages(transcript_path)
    if count < 0:
        count = 0
    if count > len(messages):
        count = 0
    return messages[count:], len(messages)
