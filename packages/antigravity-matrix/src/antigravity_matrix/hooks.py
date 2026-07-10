"""Hook handlers for Google Antigravity CLI.

Antigravity hooks receive JSON on stdin. The official Stop hook is used as
our turn-complete signal; it includes the transcript path, from which we read
and mirror the final assistant response to Matrix and TTS.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from matrix_bridge.config import load_config
from matrix_bridge.session import SessionMap

from .bridge import AntigravityBridge

STATE_DIR = Path.home() / ".ccmatrix"
ENABLED_FLAG = STATE_DIR / "antigravity-enabled"


def _is_enabled() -> bool:
    return ENABLED_FLAG.exists()


def _read_payload() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def _session_id(payload: dict[str, Any]) -> str:
    return str(payload.get("conversationId") or payload.get("conversation_id") or "")


def _workspace_cwd(payload: dict[str, Any]) -> str:
    paths = payload.get("workspacePaths") or payload.get("workspace_paths") or []
    if isinstance(paths, list) and paths:
        return str(paths[0])
    return str(payload.get("cwd") or os.getcwd())


def _transcript_path(payload: dict[str, Any]) -> Path | None:
    path = payload.get("transcriptPath") or payload.get("transcript_path")
    if not path:
        return None
    return Path(str(path))


def _register_session(payload: dict[str, Any]) -> tuple[str, str]:
    session_id = _session_id(payload)
    cwd = _workspace_cwd(payload)
    tmux_pane = os.environ.get("TMUX_PANE", "") or "unknown"

    if session_id:
        session_map = SessionMap(STATE_DIR / "antigravity-sessions.json")
        session_map.register(session_id, tmux_pane, cwd)

    return session_id, cwd


def _format_tool_call(payload: dict[str, Any]) -> str | None:
    tool_call = payload.get("toolCall") or payload.get("tool_call") or {}
    if not isinstance(tool_call, dict):
        return None

    name = str(tool_call.get("name") or "").strip()
    if not name:
        return None

    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        args = {}

    display = name.lower().replace("_", " ")
    for key in ("toolSummary", "toolAction", "CommandLine", "AbsolutePath", "TargetFile", "SearchPath", "query"):
        if key not in args or args[key] in (None, ""):
            continue
        value = str(args[key]).replace("\n", " ")
        if key in {"AbsolutePath", "TargetFile", "SearchPath"}:
            value = Path(value).name or value
        if len(value) > 120:
            value = value[:117] + "..."
        return f"● {display}(\"{value}\")"

    return f"● {display}"


async def handle_pre_invocation(payload: dict[str, Any]) -> dict:
    """Register the session, create its room, and show typing."""
    session_id, cwd = _register_session(payload)
    if not _is_enabled() or not session_id:
        return {}

    config = load_config()
    if not config:
        return {}

    async with AntigravityBridge(config) as bridge:
        await bridge.ensure_room(session_id, cwd)
        await bridge.set_typing(session_id, True)

    _ensure_daemon_running()
    return {}


async def handle_post_tool_use(payload: dict[str, Any]) -> dict:
    """Sync user/tool progress from the transcript after a tool completes."""
    session_id, cwd = _register_session(payload)
    if not _is_enabled() or not session_id:
        return {}

    config = load_config()
    if not config:
        return {}

    transcript = _transcript_path(payload)
    async with AntigravityBridge(config) as bridge:
        await bridge.ensure_room(session_id, cwd)
        if transcript:
            await bridge.catchup_from_transcript(session_id, transcript, notify_final=False)
        else:
            line = _format_tool_call(payload)
            if line:
                await bridge.send_messages(session_id, [{"role": "tool", "text": line}])
        await bridge.set_typing(session_id, True)

    _ensure_daemon_running()
    return {}


async def handle_post_invocation(payload: dict[str, Any]) -> dict:
    """Keep room state warm between model invocations.

    Do not set typing here. In Antigravity, PostInvocation can run at the end of
    the final model call, very close to the Stop hook. Treat PreInvocation and
    PostToolUse as "work is ongoing" signals, and Stop as the completion signal
    that clears typing.
    """
    session_id, cwd = _register_session(payload)
    if not _is_enabled() or not session_id:
        return {}

    config = load_config()
    if not config:
        return {}

    async with AntigravityBridge(config) as bridge:
        await bridge.ensure_room(session_id, cwd)

    _ensure_daemon_running()
    return {}


async def handle_stop(payload: dict[str, Any]) -> dict:
    """Turn-complete hook: sync final text to Matrix.

    The final assistant message is tagged cc.tts by the bridge, so the
    server-side voicehub handles voice — no local synthesis here.
    """
    session_id, cwd = _register_session(payload)
    transcript = _transcript_path(payload)
    if not _is_enabled() or not session_id:
        return {}

    config = load_config()
    if not config:
        return {}

    lock = FileLock(str(STATE_DIR / f"antigravity-stop-{session_id}.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return {}

    try:
        async with AntigravityBridge(config) as bridge:
            await bridge.ensure_room(session_id, cwd)
            try:
                if transcript:
                    await bridge.catchup_from_transcript(session_id, transcript, notify_final=True)
            finally:
                # Completion should always clear typing, even if the final hook
                # payload is missing a transcript path or transcript catch-up
                # fails. Without this, Matrix clients can show stale typing
                # until the long timeout expires.
                await bridge.set_typing(session_id, False)
    finally:
        lock.release()

    _ensure_daemon_running()
    return {}


async def handle_session_end(payload: dict[str, Any]) -> dict:
    session_id = _session_id(payload)
    if not session_id:
        return {}

    session_map = SessionMap(STATE_DIR / "antigravity-sessions.json")
    session_map.deregister(session_id)

    if _is_enabled():
        config = load_config()
        if config:
            async with AntigravityBridge(config) as bridge:
                await bridge.mark_session_ended(session_id)

    return {}


def _ensure_daemon_running() -> None:
    """Start the inbound Matrix listener daemon if needed."""
    startup_lock = FileLock(str(STATE_DIR / "antigravity_daemon_startup.lock"), timeout=0)
    try:
        startup_lock.acquire()
    except Timeout:
        return

    try:
        pid_file = STATE_DIR / "antigravity-daemon.pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text().strip()), 0)
                return
            except (OSError, ValueError):
                pass

        subprocess.Popen(
            [sys.executable, "-m", "antigravity_matrix"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        startup_lock.release()


HANDLERS = {
    "pre_invocation": handle_pre_invocation,
    "post_invocation": handle_post_invocation,
    "post_tool_use": handle_post_tool_use,
    "stop": handle_stop,
    "session_end": handle_session_end,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        print(f"Usage: python -m antigravity_matrix.hooks <{'|'.join(HANDLERS)}>", file=sys.stderr)
        return

    payload = _read_payload()
    asyncio.run(HANDLERS[sys.argv[1]](payload))


if __name__ == "__main__":
    main()
