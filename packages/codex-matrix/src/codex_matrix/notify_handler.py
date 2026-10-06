"""Handler for Codex's notify hook.

Called by Codex on agent-turn-complete. Receives JSON as first CLI argument.
Registers the session with the daemon and ensures it's running.

Codex config.toml should have:
  notify = ["/path/to/codex-matrix-notify"]

The notify payload:
  {
    "type": "agent-turn-complete",
    "thread-id": "session-uuid",
    "turn-id": "turn-uuid",
    "cwd": "/path/to/project",
    "input-messages": [...],
    "last-assistant-message": "..."
  }
"""

import json
import os
import sys
import logging
from pathlib import Path

from matrix_bridge.session import SessionMap
from matrix_bridge.tmux import pane_for_open_file
from .daemon_lifecycle import start_daemon
from .transcript import (
    find_session_file,
    has_force_mirror_marker,
    is_unmirrored_session,
    is_unmirrored_session_meta,
)

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"
ENABLED_FLAG = STATE_DIR / "codex-enabled"


def handle_notify():
    """Entry point called by the notify script."""
    if len(sys.argv) < 2:
        return

    if not ENABLED_FLAG.exists():
        return

    try:
        payload = json.loads(sys.argv[1])
    except (json.JSONDecodeError, IndexError):
        return

    thread_id = payload.get("thread-id", "")
    cwd = payload.get("cwd", "")
    inherited_tmux_pane = os.environ.get("TMUX_PANE", "")

    if not thread_id:
        return

    session_map = SessionMap(STATE_DIR / "codex-sessions.json")

    # Background subagent threads and non-interactive `codex exec` runs are
    # internal work products for the agent or script that started them.  Do not
    # register them, signal the daemon, create Matrix rooms, or trigger TTS.  If
    # a previous version already registered one, mark it inactive so inbound
    # routing and TTS guards will ignore it.
    meta = {
        "parent_thread_id": payload.get("parent_thread_id") or payload.get("parent-thread-id"),
        "thread_source": payload.get("thread_source") or payload.get("thread-source"),
        "source": payload.get("source"),
        "originator": payload.get("originator") or payload.get("thread-originator"),
    }
    session_file = find_session_file(thread_id)

    # A completion hook is often launched by Codex's long-lived app-server,
    # not by the session process itself. Its TMUX_PANE is inherited from the
    # server's launch and can belong to another live session. Never let an
    # event with no matching rollout mutate the routing map. The file watcher
    # will see a genuine rollout when it exists and can establish ownership
    # from its open descriptor.
    if not session_file:
        if is_unmirrored_session_meta(meta):
            session_map.deregister(thread_id)
        logger.warning(
            "Ignoring Codex notify for %s: no matching rollout; preserving pane ownership",
            thread_id[:8],
        )
        return

    # The opt-in is checked first and separately: the metadata rules are
    # deliberately redundant across payload and file, so a session that asked to
    # be mirrored must clear both of them, not just the file-based one.
    forced = bool(session_file and has_force_mirror_marker(session_file))
    if not forced and (
        is_unmirrored_session_meta(meta) or (session_file and is_unmirrored_session(session_file))
    ):
        session_map.deregister(thread_id)
        logger.info(f"Ignoring unmirrored Codex session {thread_id[:8]} (cwd: {cwd})")
        return

    # The process holding the rollout open is the authoritative source of the
    # pane binding. The hook subprocess's environment can be inherited from an
    # app-server serving several threads. When provenance is unavailable, keep
    # the mapping provisional rather than retiring the inherited pane's owner.
    tmux_pane = pane_for_open_file(session_file) or ""
    if tmux_pane:
        logger.info(f"Resolved pane {tmux_pane} for {thread_id[:8]} from the process table")
    elif inherited_tmux_pane:
        logger.warning(
            "No verified tmux pane for Codex session %s; ignoring inherited pane %s",
            thread_id[:8],
            inherited_tmux_pane,
        )

    if not tmux_pane:
        logger.warning(
            f"No verified tmux pane for Codex session {thread_id[:8]}; "
            "entry stays provisional and inbound replies cannot be routed"
        )

    session_map.register(thread_id, tmux_pane or "unknown", cwd)
    logger.info(f"Registered Codex session {thread_id[:8]} (pane: {tmux_pane}, cwd: {cwd})")

    # Signal the daemon that this session has new data. Include turn_id so the
    # daemon can dedupe overlapping completion signals from the transcript and
    # the notify hook.
    signal_file = STATE_DIR / "codex-notify-signal"
    signal_file.write_text(json.dumps({
        "thread_id": thread_id,
        "turn_id": payload.get("turn-id", ""),
        "cwd": cwd,
        "tmux_pane": tmux_pane,
        "last_assistant_message": payload.get("last-assistant-message", ""),
    }))

    # Ensure daemon is running
    _ensure_daemon_running()


def _ensure_daemon_running():
    """Start the Codex daemon if not already running."""
    result = start_daemon(STATE_DIR, required_enabled_flag=ENABLED_FLAG)
    if result.state == "failed":
        logger.error("Codex daemon failed to become ready after notify: %s", result.detail)


if __name__ == "__main__":
    handle_notify()
