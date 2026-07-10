"""Parse Codex CLI JSONL session files into user/assistant message pairs.

Codex stores sessions at:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<session-uuid>.jsonl

Event types in the JSONL:
  - session_meta: session metadata (id, cwd, model, etc.)
  - response_item: messages, function calls, tool outputs
  - event_msg: lifecycle events (task_started, etc.)
  - turn_context: turn metadata
  - compacted: context compression markers
"""

import json
from pathlib import Path


CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
HIDDEN_USER_MARKERS = (
    "PAPER_VOICE_DAILY_RUN_ID=",
)


def _is_hidden_user_text(text: str) -> bool:
    """Return True for automation prompts that should not be mirrored to Matrix."""
    return any(marker in text for marker in HIDDEN_USER_MARKERS)


def is_unmirrored_session_meta(meta: dict | None) -> bool:
    """Return True when a Codex session is internal agent work.

    Multi-agent background threads are addressed to the parent agent, not the
    human user.  They should not get Matrix rooms, push notifications, or TTS.
    Current Codex session metadata marks these with ``thread_source=subagent``
    plus a parent thread id; keep the checks intentionally redundant so older
    and newer metadata shapes are both covered.
    """
    if not meta:
        return False

    if meta.get("thread_source") in {"subagent", "background"}:
        return True

    if meta.get("parent_thread_id"):
        return True

    source = meta.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return True

    return False


def is_unmirrored_session(session_path: Path) -> bool:
    """Return True when a Codex session file should not be mirrored to Matrix."""
    return is_unmirrored_session_meta(extract_session_meta(session_path))


def has_hidden_user_marker(session_path: Path) -> bool:
    """Return True if the session contains a hidden automation prompt marker."""
    if not session_path.exists():
        return False
    try:
        for line in session_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(marker in line for marker in HIDDEN_USER_MARKERS):
                return True
    except OSError:
        return False
    return False


def extract_latest_assistant_after_last_hidden_marker(session_path: Path) -> str | None:
    """Return the latest assistant text after the most recent hidden prompt.

    This is intentionally stricter than "latest assistant in the file" so a
    hidden automation turn that fails early, such as from a rate limit, does
    not replay yesterday's completed brief.
    """
    if not session_path.exists():
        return None

    latest: str | None = None
    seen_marker = False

    try:
        lines = session_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") != "response_item":
            continue

        payload = obj.get("payload", {})
        item_type = payload.get("type")
        role = payload.get("role", "")

        if item_type == "message" and role == "user":
            for block in payload.get("content", []):
                if block.get("type") != "input_text":
                    continue
                text = block.get("text", "")
                if _is_hidden_user_text(text):
                    seen_marker = True
                    latest = None

        elif seen_marker and item_type == "message" and role == "assistant":
            chunks = []
            for block in payload.get("content", []):
                if block.get("type") == "output_text":
                    text = block.get("text", "").strip()
                    if text:
                        chunks.append(text)
            if chunks:
                latest = "\n\n".join(chunks)

    return latest


def find_session_file(thread_id: str) -> Path | None:
    """Find the JSONL session file for a Codex thread ID.

    Scans ~/.codex/sessions/ for a file whose name ends with the thread ID.
    """
    if not CODEX_SESSIONS_DIR.exists():
        return None

    # Files are named: rollout-<timestamp>-<thread-id>.jsonl
    for path in CODEX_SESSIONS_DIR.rglob(f"*-{thread_id}.jsonl"):
        return path

    return None


def extract_session_meta(session_path: Path) -> dict | None:
    """Extract session metadata (id, cwd) from the first line."""
    with open(session_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "session_meta":
                    payload = obj["payload"]
                    return {
                        "id": payload.get("id", ""),
                        "session_id": payload.get("session_id", ""),
                        "parent_thread_id": payload.get("parent_thread_id"),
                        "cwd": payload.get("cwd", ""),
                        "model": payload.get("model_provider", ""),
                        "source": payload.get("source"),
                        "thread_source": payload.get("thread_source"),
                        "agent_nickname": payload.get("agent_nickname"),
                        "agent_role": payload.get("agent_role"),
                    }
            except (json.JSONDecodeError, KeyError):
                continue
    return None


def extract_messages(session_path: Path) -> list[dict]:
    """Extract user and assistant text messages from a Codex session JSONL.

    Returns a list of dicts with keys: role ("user" or "assistant"), text.
    Skips developer messages, reasoning, function calls, and event metadata.
    """
    messages = []

    with open(session_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            if obj.get("type") != "response_item":
                continue

            payload = obj.get("payload", {})
            item_type = payload.get("type")
            role = payload.get("role", "")

            if item_type == "message" and role == "user":
                # User messages: content is a list of {type: "input_text", text: "..."}
                # Skip system/AGENTS.md injected content (very long, starts with #)
                content = payload.get("content", [])
                for block in content:
                    if block.get("type") == "input_text":
                        text = block.get("text", "").strip()
                        # Skip injected AGENTS.md / system instructions
                        if (
                            text
                            and not text.startswith("# AGENTS.md")
                            and not text.startswith("<permissions")
                            and not text.startswith("<environment_context")
                            and not _is_hidden_user_text(text)
                        ):
                            messages.append({"role": "user", "text": text})

            elif item_type == "message" and role == "assistant":
                # Assistant messages: content is a list of {type: "output_text", text: "..."}
                # May have phase: "commentary" (intermediary) or no phase (final)
                content = payload.get("content", [])
                for block in content:
                    if block.get("type") == "output_text":
                        text = block.get("text", "").strip()
                        if text:
                            messages.append({"role": "assistant", "text": text})

            elif item_type == "function_call":
                # Tool calls: {name: "exec_command", arguments: "{\"cmd\": ...}"}
                name = payload.get("name", "")
                args_str = payload.get("arguments", "{}")
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    args = {}
                line_text = _format_tool_call(name, args)
                if line_text:
                    messages.append({"role": "tool", "text": line_text})

    return messages


def extract_messages_from_offset(session_path: Path, byte_offset: int) -> tuple[list[dict], int]:
    """Extract messages starting from a byte offset in the file.

    Returns (messages, new_byte_offset) so the caller can track progress.
    """
    messages = []

    with open(session_path, "rb") as f:
        f.seek(byte_offset)
        data = f.read()
        new_offset = byte_offset + len(data)

    for raw_line in data.decode("utf-8", errors="replace").split("\n"):
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            obj = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if obj.get("type") == "event_msg":
            payload = obj.get("payload", {})
            if payload.get("type") == "task_complete":
                # Surface task completion as a control event so the daemon can
                # flush the final assistant reply even if the notify hook is
                # late or absent for this session.
                messages.append({
                    "role": "control",
                    "event": "task_complete",
                    "turn_id": payload.get("turn_id", ""),
                })
            continue

        if obj.get("type") != "response_item":
            continue

        payload = obj.get("payload", {})
        item_type = payload.get("type")
        role = payload.get("role", "")

        if item_type == "message" and role == "user":
            content = payload.get("content", [])
            for block in content:
                if block.get("type") == "input_text":
                    text = block.get("text", "").strip()
                    if (
                        text
                        and not text.startswith("# AGENTS.md")
                        and not text.startswith("<permissions")
                        and not text.startswith("<environment_context")
                        and not _is_hidden_user_text(text)
                    ):
                        messages.append({"role": "user", "text": text})

        elif item_type == "message" and role == "assistant":
            content = payload.get("content", [])
            for block in content:
                if block.get("type") == "output_text":
                    text = block.get("text", "").strip()
                    if text:
                        messages.append({"role": "assistant", "text": text})

        elif item_type == "function_call":
            name = payload.get("name", "")
            args_str = payload.get("arguments", "{}")
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {}
            line_text = _format_tool_call(name, args)
            if line_text:
                messages.append({"role": "tool", "text": line_text})

    return messages, new_offset


def _format_tool_call(name: str, args: dict) -> str | None:
    """Format a Codex tool call into a one-liner like: ● exec_command("ls -la")"""
    TOOLS = {
        "exec_command": ("Bash", "cmd"),
        "apply_patch": ("Patch", None),
        "read_file": ("Read", "path"),
        "write_file": ("Write", "path"),
    }

    display_name, arg_key = TOOLS.get(name, (name, None))

    if arg_key and arg_key in args:
        arg = str(args[arg_key]).replace("\n", " ")
        if len(arg) > 120:
            arg = arg[:117] + "..."
        return f'● {display_name}("{arg}")'

    if name == "apply_patch":
        return "● Patch (apply_patch)"

    return f"● {display_name}"
