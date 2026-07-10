"""Hook handlers — called by Claude Code on session events.

Each hook receives JSON on stdin with event-specific fields.
Posts messages to Matrix via the bridge client.
"""

import json
import sys
import asyncio
import os
from pathlib import Path

from matrix_bridge.config import load_config
from matrix_bridge.session import SessionMap
from .bridge import MatrixBridge, STATUS_ACTIVE


STATE_DIR = Path.home() / ".ccmatrix"
ENABLED_FLAG = STATE_DIR / "enabled"


def _is_enabled() -> bool:
    """Check if the Matrix bridge is enabled."""
    return ENABLED_FLAG.exists()


async def handle_session_start(payload: dict) -> dict:
    """Map session_id to tmux pane, create Matrix room for session."""
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    tmux_pane = os.environ.get("TMUX_PANE", "")

    if not session_id or not tmux_pane:
        return {}

    # Persist session → tmux mapping (always, even when disabled)
    session_map = SessionMap(STATE_DIR / "sessions.json")
    session_map.register(session_id, tmux_pane, cwd)

    if not _is_enabled():
        return {}

    # Create Matrix room for this session (or reactivate existing one on resume)
    config = load_config()
    if config:
        async with MatrixBridge(config) as bridge:
            entry = bridge.session_map.get(session_id)
            if entry and entry.room_id:
                # Resumed session — reactivate the existing room
                await bridge.update_room_status(session_id, STATUS_ACTIVE)
            else:
                await bridge.create_room(session_id, cwd)

    # Start inbound daemon if not running
    _ensure_daemon_running()

    return {}


def _format_tool_use(tool_name: str, tool_input: dict) -> str:
    """Format a tool use into a one-liner like: ● Read("src/config.py")"""
    # Map tool names to display names and which input field to show
    TOOLS = {
        "Bash": ("Bash", "command"),
        "Read": ("Read", "file_path"),
        "Write": ("Write", "file_path"),
        "Edit": ("Edit", "file_path"),
        "Glob": ("Glob", "pattern"),
        "Grep": ("Grep", "pattern"),
        "WebSearch": ("Web Search", "query"),
        "WebFetch": ("Web Fetch", "url"),
        "Agent": ("Agent", "description"),
        "Skill": ("Skill", "skill"),
    }

    display_name, input_key = TOOLS.get(tool_name, (tool_name, None))

    if input_key and input_key in tool_input:
        arg = str(tool_input[input_key]).replace("\n", " ")
        if len(arg) > 120:
            arg = arg[:117] + "..."
        return f'● {display_name}("{arg}")'

    return f"● {display_name}"


async def _sync_to_matrix(payload: dict) -> dict:
    """Sync any new transcript messages to Matrix (text only)."""
    if not _is_enabled():
        return {}

    session_id = payload.get("session_id", "")
    if not session_id:
        return {}

    config = load_config()
    if config:
        async with MatrixBridge(config) as bridge:
            await bridge.catchup_from_transcript(session_id)

            # Set typing indicator so the Matrix client shows "thinking..."
            entry = bridge.session_map.get(session_id)
            if entry and entry.room_id:
                await bridge.bot_client.room_typing(
                    entry.room_id, config.user_id, typing=True, timeout=120000,
                )

    return {}


async def handle_pre_tool_use(payload: dict) -> dict:
    """Sync transcript, then send a tool-use one-liner to the room."""
    if not _is_enabled():
        return {}

    session_id = payload.get("session_id", "")
    if not session_id:
        return {}

    config = load_config()
    if not config:
        return {}

    async with MatrixBridge(config) as bridge:
        await bridge.catchup_from_transcript(session_id)

        entry = bridge.session_map.get(session_id)
        if entry and entry.room_id:
            # Send tool one-liner
            tool_name = payload.get("tool_name", "")
            if tool_name:
                line = _format_tool_use(tool_name, payload.get("tool_input", {}))
                await bridge.bot_client.room_send(entry.room_id, line, catchup=True)

            # Keep typing indicator active
            await bridge.bot_client.room_typing(
                entry.room_id, config.user_id, typing=True, timeout=120000,
            )

    return {}


async def handle_stop(payload: dict) -> dict:
    """Sync text messages; the final assistant message is tagged for server TTS."""
    if not _is_enabled():
        return {}

    session_id = payload.get("session_id", "")
    if not session_id:
        return {}

    config = load_config()
    if not config:
        return {}

    async with MatrixBridge(config) as bridge:
        # Sync all text messages — the final assistant message triggers a
        # notification and is tagged cc.tts for server-side voice synthesis.
        await bridge.catchup_from_transcript(session_id, notify_final=True)

        # Clear typing indicator once the sync is done
        entry = bridge.session_map.get(session_id)
        if entry and entry.room_id:
            await bridge.bot_client.room_typing(entry.room_id, config.user_id, typing=False)

    return {}


async def handle_notification(payload: dict) -> dict:
    """Notification hook — disabled, too noisy."""
    return {}


async def handle_session_end(payload: dict) -> dict:
    """Mark session as ended in Matrix room."""
    session_id = payload.get("session_id", "")

    if not session_id:
        return {}

    # Always clean up session map, even when disabled
    session_map = SessionMap(STATE_DIR / "sessions.json")
    session_map.deregister(session_id)

    if not _is_enabled():
        return {}

    config = load_config()
    if config:
        async with MatrixBridge(config) as bridge:
            await bridge.mark_session_ended(session_id)

    return {}


def _ensure_daemon_running():
    """Start the inbound listener daemon if not already running.

    Uses a startup lock to prevent concurrent hook calls from
    spawning multiple daemons during the startup window.
    """
    from filelock import FileLock, Timeout

    startup_lock = FileLock(str(STATE_DIR / "daemon_startup.lock"), timeout=0)
    try:
        startup_lock.acquire()
    except Timeout:
        return  # Another hook call is already starting the daemon

    try:
        # Check if daemon is running via its own lock
        pid_file = STATE_DIR / "daemon.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, 0)
                return  # Already running
            except OSError:
                pass  # Stale PID file

        # Start daemon in background
        import subprocess
        subprocess.Popen(
            [sys.executable, "-m", "claude_code_matrix.daemon"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        startup_lock.release()


# CLI entrypoint — called by hook commands
HANDLERS = {
    "session_start": handle_session_start,
    "stop": handle_stop,
    "user_prompt_submit": _sync_to_matrix,
    "pre_tool_use": handle_pre_tool_use,
    "notification": handle_notification,
    "session_end": handle_session_end,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        print(f"Usage: python -m claude_code_matrix.hooks <{'|'.join(HANDLERS)}>", file=sys.stderr)
        sys.exit(1)

    event = sys.argv[1]
    payload = json.load(sys.stdin)
    handler = HANDLERS[event]
    result = asyncio.run(handler(payload))
    json.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
