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
from .transcript import (
    find_session_file,
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
    tmux_pane = os.environ.get("TMUX_PANE", "")

    if not thread_id:
        return

    session_map = SessionMap(STATE_DIR / "codex-sessions.json")

    # Background subagent threads are internal work products for the parent
    # agent.  Do not register them, signal the daemon, create Matrix rooms, or
    # trigger TTS.  If a previous version already registered one, mark it
    # inactive so inbound routing and TTS guards will ignore it.
    meta = {
        "parent_thread_id": payload.get("parent_thread_id") or payload.get("parent-thread-id"),
        "thread_source": payload.get("thread_source") or payload.get("thread-source"),
        "source": payload.get("source"),
    }
    session_file = find_session_file(thread_id)
    if is_unmirrored_session_meta(meta) or (session_file and is_unmirrored_session(session_file)):
        session_map.deregister(thread_id)
        logger.info(f"Ignoring unmirrored Codex session {thread_id[:8]} (cwd: {cwd})")
        return

    # Register session so the daemon knows about it. The notify hook carries
    # the authoritative tmux pane binding for the live Codex process, so this
    # call is allowed to refresh an existing session discovered provisionally
    # by the file watcher.
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
    }))

    # Ensure daemon is running
    _ensure_daemon_running()


def _ensure_daemon_running():
    """Start the Codex daemon if not already running."""
    from filelock import FileLock, Timeout

    startup_lock = FileLock(str(STATE_DIR / "codex_daemon_startup.lock"), timeout=0)
    try:
        startup_lock.acquire()
    except Timeout:
        return

    try:
        pid_file = STATE_DIR / "codex-daemon.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, 0)
                return  # Already running
            except OSError:
                pass  # Stale PID file

        import subprocess
        subprocess.Popen(
            [sys.executable, "-m", "codex_matrix"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        startup_lock.release()


if __name__ == "__main__":
    handle_notify()
