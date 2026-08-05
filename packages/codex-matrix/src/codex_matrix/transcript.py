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

# Opt back in to mirroring from inside a non-interactive run. See
# has_force_mirror_marker for why this is a marker rather than an env var.
FORCE_MIRROR_MARKERS = (
    "CCMATRIX_FORCE_MIRROR",
)


def _is_hidden_user_text(text: str) -> bool:
    """Return True for automation prompts that should not be mirrored to Matrix."""
    return any(marker in text for marker in HIDDEN_USER_MARKERS)


def is_noninteractive_session_meta(meta: dict | None) -> bool:
    """Return True when a Codex session is a `codex exec` run, not a person typing.

    `codex exec` is the non-interactive entry point: a script starts it, it runs
    one turn, it exits. Nobody is at a terminal, and the bridge's inbound path
    types phone replies into a tmux pane — an exec process ignores keystrokes,
    so its room can never be answered. Mirroring one is write-only noise: a
    room, a push notification and a spoken reply for output addressed to the
    script that launched it. A single automation round can open several.

    The signal is metadata Codex itself writes into the rollout's ``session_meta``
    line, which is what makes this usable at all: the daemon discovers sessions
    with a filesystem watcher and never sees the spawning process's environment,
    so an environment variable cannot reach this decision. Both fields are
    checked because both have moved: across 514 rollouts on one machine,
    ``source`` is ``"exec"`` for exec runs and ``"cli"`` for interactive ones,
    while ``originator`` is ``codex_exec`` for exec runs and ``codex-tui`` or
    (older builds) ``codex_cli_rs`` for interactive ones.

    ``source`` is not always a string — for subagent threads it is a dict — so
    it is type-checked before comparison rather than trusted.
    """
    if not meta:
        return False

    source = meta.get("source")
    if isinstance(source, str) and source.strip().lower() == "exec":
        return True

    originator = meta.get("originator")
    if isinstance(originator, str) and originator.strip().lower() in {"codex_exec", "codex-exec"}:
        return True

    return False


def is_unmirrored_session_meta(meta: dict | None) -> bool:
    """Return True when a Codex session should not reach the human's phone.

    Two independent reasons, both of them "this output is addressed to a
    machine, not to the person holding the phone":

    * internal agent work — multi-agent background threads belong to the parent
      agent. Current Codex metadata marks these with ``thread_source=subagent``
      plus a parent thread id; the checks are intentionally redundant so older
      and newer metadata shapes are both covered.
    * a non-interactive `codex exec` run — see is_noninteractive_session_meta.

    This is the metadata half of the decision. Callers holding the session file
    should use is_unmirrored_session, which also honours the opt-in marker.
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

    if is_noninteractive_session_meta(meta):
        return True

    return False


def has_force_mirror_marker(session_path: Path) -> bool:
    """Return True when a session explicitly asks to be mirrored anyway.

    The escape hatch for the rules above: put ``CCMATRIX_FORCE_MIRROR``
    anywhere in the prompt and the session gets a room, a notification and TTS
    even though it is a scripted run.

        codex exec "CCMATRIX_FORCE_MIRROR Summarise today's alerts"

    It is a marker in the prompt rather than an environment variable on purpose,
    and this is the mechanism's one real limit: the daemon learns about most
    sessions from a filesystem watcher over ``~/.codex/sessions/``, in a
    long-lived process that never sees the environment of whatever spawned the
    CLI. The rollout is the only channel that reaches it. Codex does write an
    ``<environment_context>`` block into the transcript, but it carries cwd,
    shell, date, timezone and filesystem roots only — no environment variables —
    so nothing an exporter sets can be recovered from the file. A prompt marker
    can, and it follows HIDDEN_USER_MARKERS, which solves the same problem in
    the same place.

    The cost is that the marker is part of the prompt the model reads. Keep it
    on its own line or at the very start so it reads as a directive to the
    tooling.
    """
    if not session_path.exists():
        return False
    try:
        for line in session_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if any(marker in line for marker in FORCE_MIRROR_MARKERS):
                return True
    except OSError:
        return False
    return False


def is_unmirrored_session(session_path: Path) -> bool:
    """Return True when a Codex session file should not be mirrored to Matrix.

    The marker is only looked for once the metadata has already decided to
    suppress, so the common path stays a single-line read rather than a scan of
    the whole rollout.
    """
    if not is_unmirrored_session_meta(extract_session_meta(session_path)):
        return False
    return not has_force_mirror_marker(session_path)


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
                        "originator": payload.get("originator"),
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
