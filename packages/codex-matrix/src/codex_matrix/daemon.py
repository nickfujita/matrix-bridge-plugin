"""Codex Matrix daemon — bidirectional bridge between Codex and Matrix.

Combines:
1. Matrix /sync polling for inbound messages (Matrix → tmux)
2. JSONL file watching for outbound messages (Codex session → Matrix)

The daemon discovers Codex sessions via the notify hook signal file.
"""

import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from matrix_bridge.config import load_config, MatrixConfig
from matrix_bridge.matrix import MatrixClient
from matrix_bridge.session import SessionMap
from matrix_bridge.tmux import pane_current_command, send_keys

from .bridge import CodexBridge
from .transcript import (
    extract_latest_assistant_after_last_hidden_marker,
    extract_session_meta,
    find_session_file,
    has_hidden_user_marker,
    is_unmirrored_session,
)
from .watcher import SessionWatcher

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"
CODEX_ACTIVE_COMMANDS = {"codex", "node"}
SESSION_CLEANUP_INTERVAL_SECONDS = 30
UNKNOWN_PANE_STALE_SECONDS = 10 * 60


class CodexDaemon:
    """Bidirectional Codex ↔ Matrix bridge daemon."""

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.bridge = CodexBridge(config)
        # Separate Matrix client for inbound polling (uses bot token)
        self.poll_client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "codex-sessions.json")
        self.watcher: SessionWatcher | None = None
        self.running = True
        self.next_batch: str | None = None
        # Track which sessions we're actively watching
        self.watched_sessions: set[str] = set()
        # Buffer assistant messages until turn-complete to avoid notification spam
        self._pending_assistant: dict[str, list[dict]] = {}
        # Dedupe turn-complete handling when both transcript and notify fire.
        self._last_completed_turn: dict[str, str] = {}
        self._inflight_turns: set[tuple[str, str]] = set()

    async def start(self) -> None:
        """Start the daemon — runs both inbound and outbound loops."""
        async with self.bridge:
            async with self.poll_client:
                try:
                    loop = asyncio.get_event_loop()
                    self.watcher = SessionWatcher(self._on_file_messages)
                    self.watcher.start(loop)

                    # Discover any existing active sessions
                    await self._discover_sessions()

                    await asyncio.gather(
                        self._matrix_poll_loop(),
                        self._signal_watch_loop(),
                        self._session_cleanup_loop(),
                    )
                finally:
                    if self.watcher:
                        self.watcher.stop()

    async def _discover_sessions(self) -> None:
        """Find and watch any active sessions on startup."""
        for entry in self.session_map.active_sessions():
            session_file = find_session_file(entry.session_id)
            if session_file and is_unmirrored_session(session_file):
                await self._retire_session(entry.session_id, "unmirrored background thread")
                continue
            if session_file and entry.session_id not in self.watched_sessions:
                # Create Matrix room if session was registered but never got one
                if not entry.room_id:
                    await self.bridge.create_room(entry.session_id, entry.cwd)
                self.watcher.watch_file(session_file)
                self.watched_sessions.add(entry.session_id)
                logger.info(f"Resumed watching session {entry.session_id[:8]}")

    async def _signal_watch_loop(self) -> None:
        """Watch for notify hook signals to discover new sessions and flush turns.

        The notify hook fires on agent-turn-complete. For new sessions, we set up
        watching and create a Matrix room. For existing sessions, we flush the
        buffered assistant messages (the final one is tagged cc.tts for
        server-side voice), only on turn completion, not on every intermediate
        file write.
        """
        signal_file = STATE_DIR / "codex-notify-signal"

        while self.running:
            try:
                if signal_file.exists():
                    import json
                    data = json.loads(signal_file.read_text())
                    signal_file.unlink(missing_ok=True)

                    thread_id = data.get("thread_id", "")
                    if thread_id and thread_id not in self.watched_sessions:
                        await self._setup_session(thread_id, data.get("cwd", ""), data.get("tmux_pane", ""))
                    elif thread_id:
                        # Turn complete for an existing session — generate TTS
                        await self._on_turn_complete(thread_id, data.get("turn_id", ""))
            except Exception as e:
                logger.error(f"Signal watch error: {e}")

            await asyncio.sleep(2)

    async def _on_turn_complete(self, thread_id: str, turn_id: str = "") -> None:
        """Flush buffered assistant messages when a turn completes.

        Assistant messages are buffered during file watching (sent silently as
        m.notice for progress).  On turn-complete we send them with
        notify_final=True so the last one triggers a push notification and is
        tagged cc.tts for server-side voice synthesis.
        """
        inflight_key: tuple[str, str] | None = None
        if turn_id:
            last_turn_id = self._last_completed_turn.get(thread_id)
            if last_turn_id == turn_id:
                logger.info(f"Skipping duplicate turn-complete for {thread_id[:8]} ({turn_id[:8]})")
                return
            inflight_key = (thread_id, turn_id)
            if inflight_key in self._inflight_turns:
                logger.info(f"Skipping in-flight duplicate turn-complete for {thread_id[:8]} ({turn_id[:8]})")
                return
            # Mark the turn as in-flight before any awaits. Transcript-based
            # completion and notify-hook completion can arrive almost
            # simultaneously; if we wait until after Matrix/TTS work finishes,
            # the second callback can synthesize a duplicate audio clip.
            self._inflight_turns.add(inflight_key)

        try:
            entry = self.session_map.get(thread_id)
            if not entry or not entry.room_id or not entry.active:
                return

            session_file = find_session_file(thread_id)
            if session_file and is_unmirrored_session(session_file):
                await self._retire_session(thread_id, "unmirrored background thread")
                return

            # Flush buffered assistant messages — last one triggers notification
            pending = self._pending_assistant.pop(thread_id, [])
            final_only = bool(session_file and has_hidden_user_marker(session_file))
            logger.info(
                "Handling turn-complete for %s (turn=%s, pending=%d)",
                thread_id[:8],
                turn_id[:8] if turn_id else "missing",
                len(pending),
            )
            if pending:
                to_send = [pending[-1]] if final_only else pending
                await self.bridge.send_messages(thread_id, to_send, notify_final=True)
                logger.info(f"Flushed {len(to_send)} assistant msgs for {thread_id[:8]} with notification")
            elif final_only and session_file:
                recovered_text = extract_latest_assistant_after_last_hidden_marker(session_file)
                if recovered_text:
                    await self.bridge.send_messages(
                        thread_id,
                        [{"role": "assistant", "text": recovered_text}],
                        notify_final=True,
                    )
                    pending = [{"role": "assistant", "text": recovered_text}]
                    logger.info(f"Recovered final assistant msg for {thread_id[:8]} with notification")

            await self.bridge.set_typing(thread_id, False)

            # Voice is now server-side: the final assistant message sent above
            # carries the cc.tts tag, so no local synthesis happens here.

            if turn_id:
                self._last_completed_turn[thread_id] = turn_id
        finally:
            if inflight_key is not None:
                self._inflight_turns.discard(inflight_key)

    async def _setup_session(self, thread_id: str, cwd: str, tmux_pane: str) -> None:
        """Set up watching and Matrix room for a new Codex session."""
        session_file = find_session_file(thread_id)
        if not session_file:
            logger.warning(f"Session file not found for {thread_id[:8]}")
            return

        if is_unmirrored_session(session_file):
            await self._retire_session(thread_id, "unmirrored background thread")
            return

        # Extract metadata if not already registered
        entry = self.session_map.get(thread_id)
        if not entry:
            meta = extract_session_meta(session_file)
            if meta:
                cwd = meta.get("cwd", cwd)
            self.session_map.register(thread_id, tmux_pane, cwd)

        # Create Matrix room if needed
        entry = self.session_map.get(thread_id)
        if entry and not entry.room_id:
            await self.bridge.create_room(thread_id, entry.cwd or cwd)
        elif entry and entry.room_id:
            await self.bridge.refresh_branch_if_changed(thread_id)

        # Start watching the session file
        self.watcher.watch_file(session_file)
        self.watched_sessions.add(thread_id)
        logger.info(f"Now watching Codex session {thread_id[:8]}")

    async def _on_file_messages(self, path: Path, messages: list[dict]) -> None:
        """Called when new messages are detected in a session JSONL file.

        Identifies the session from the filename and forwards messages to Matrix.
        Auto-registers new sessions and creates Matrix rooms on first sight.
        """
        # Extract thread ID from filename: rollout-<timestamp>-<uuid>.jsonl
        filename = path.stem  # e.g. "rollout-2026-03-18T12-50-14-019d00ff-..."
        # The UUID is the last 36 chars of the stem
        thread_id = filename[-36:] if len(filename) >= 36 else ""

        if not thread_id:
            return

        if is_unmirrored_session(path):
            await self._retire_session(thread_id, "unmirrored background thread")
            return

        entry = self.session_map.get(thread_id)
        if entry and not entry.active:
            return

        # Auto-register new sessions discovered via file watcher
        if not entry:
            meta = extract_session_meta(path)
            cwd = meta.get("cwd", "") if meta else ""
            # The daemon's own environment is not tied to the originating
            # Codex pane. Register a provisional entry and let the notify hook
            # backfill the real pane when it fires.
            self.session_map.register(thread_id, "unknown", cwd)
            logger.info(f"Auto-registered Codex session {thread_id[:8]} from file watcher")
            entry = self.session_map.get(thread_id)

        # Create Matrix room if needed
        if entry and not entry.room_id:
            await self.bridge.create_room(thread_id, entry.cwd)
            entry = self.session_map.get(thread_id)
        elif entry and entry.room_id:
            await self.bridge.refresh_branch_if_changed(thread_id)

        if not entry or not entry.room_id or not entry.active:
            return

        control_events = [m for m in messages if m.get("role") == "control"]
        content_messages = [m for m in messages if m.get("role") != "control"]
        final_only = has_hidden_user_marker(path)

        # Split messages: send tool/user immediately (silent), buffer assistant
        # Assistant messages are held until turn-complete so only the final one
        # triggers a push notification (avoids notification spam on every batch).
        immediate = [] if final_only else [m for m in content_messages if m["role"] != "assistant"]
        assistant_msgs = [m for m in content_messages if m["role"] == "assistant"]

        if assistant_msgs:
            self._pending_assistant.setdefault(thread_id, []).extend(assistant_msgs)
            logger.info(f"Buffered {len(assistant_msgs)} assistant msgs from {thread_id[:8]}")

        if immediate:
            sent = await self.bridge.send_messages(thread_id, immediate)
            if sent > 0:
                logger.info(f"Forwarded {sent} messages from {thread_id[:8]} to Matrix")

        # Update typing indicator
        if assistant_msgs:
            # Assistant wrote something — clear typing, turn-complete will flush
            await self.bridge.set_typing(thread_id, False)
        elif immediate:
            # Still working (tool calls only) — show typing
            await self.bridge.set_typing(thread_id, True)

        seen_turn_ids: set[str] = set()
        for event in control_events:
            if event.get("event") != "task_complete":
                continue
            turn_id = event.get("turn_id", "")
            key = turn_id or "__missing__"
            if key in seen_turn_ids:
                continue
            seen_turn_ids.add(key)
            # The transcript is a second source of truth for turn completion.
            # Keep listening here so final reply delivery still works if the
            # notify hook is delayed or misses a callback.
            await self._on_turn_complete(thread_id, turn_id)

    async def _retire_session(self, session_id: str, reason: str) -> None:
        """Mark a session inactive and stop all outbound Matrix/TTS work."""
        entry = self.session_map.get(session_id)
        self._pending_assistant.pop(session_id, None)
        self.watched_sessions.discard(session_id)

        if entry and entry.room_id:
            try:
                await self.bridge.mark_session_ended(session_id)
            except Exception as e:
                logger.warning(f"Failed to mark Codex session {session_id[:8]} ended: {e}")

        self.session_map.deregister(session_id)
        logger.info(f"Retired Codex session {session_id[:8]}: {reason}")

    async def _session_cleanup_loop(self) -> None:
        """Mark Codex Matrix rooms inactive when their tmux pane is gone."""
        while self.running:
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL_SECONDS)
            try:
                await self._cleanup_ended_sessions()
            except Exception as e:
                logger.error(f"Session cleanup error: {e}")

    async def _cleanup_ended_sessions(self) -> None:
        """Retire active sessions whose originating Codex pane has ended."""
        for entry in self.session_map.active_sessions():
            if not entry.tmux_pane or entry.tmux_pane == "unknown":
                session_file = find_session_file(entry.session_id)
                if session_file and time.time() - session_file.stat().st_mtime > UNKNOWN_PANE_STALE_SECONDS:
                    await self._retire_session(entry.session_id, "stale provisional session without tmux pane")
                elif not session_file and entry.started_at and time.time() - entry.started_at > UNKNOWN_PANE_STALE_SECONDS:
                    await self._retire_session(entry.session_id, "stale provisional session without session file")
                continue

            command = pane_current_command(entry.tmux_pane)
            if command and command.lower() in CODEX_ACTIVE_COMMANDS:
                continue

            reason = "tmux pane missing" if not command else f"pane command is {command}"
            await self._retire_session(entry.session_id, reason)

    # --- Inbound: Matrix → tmux ---

    async def _matrix_poll_loop(self) -> None:
        """Long-poll Matrix /sync for inbound messages."""
        logger.info("Starting Matrix poll loop for inbound messages")

        # Initial sync to skip old messages
        data = await self.poll_client.sync(timeout=10000)
        self.next_batch = data.get("next_batch")

        while self.running:
            try:
                data = await self.poll_client.sync(
                    since=self.next_batch, timeout=30000,
                )
                self.next_batch = data.get("next_batch")
                await self._process_sync(data)
            except asyncio.TimeoutError:
                # Sync exceeded its hard timeout — connection is likely
                # half-open. Drop the aiohttp session so the next sync
                # opens a fresh TCP connection.
                logger.warning("Matrix sync timed out, reconnecting")
                await self.poll_client.reconnect()
            except Exception as e:
                logger.error(f"Matrix sync error: {type(e).__name__}: {e}")
                await asyncio.sleep(5)

    async def _process_sync(self, data: dict) -> None:
        """Route inbound Matrix messages to the correct tmux pane."""
        rooms = data.get("rooms", {}).get("join", {})

        for room_id, room_data in rooms.items():
            timeline = room_data.get("timeline", {})
            for event in timeline.get("events", []):
                await self._handle_inbound(room_id, event)

    async def _handle_inbound(self, room_id: str, event: dict) -> None:
        """Handle a single inbound Matrix event."""
        if event.get("type") != "m.room.message":
            return

        if event.get("sender") == self.config.user_id:
            return

        content = event.get("content", {})
        msgtype = content.get("msgtype")

        if content.get("cc.catchup"):
            return

        # Only text is handled. Voice arrives as ordinary @admin text via the
        # server-side voicehub STT appservice, so m.audio is ignored here.
        if msgtype == "m.text":
            await self._on_inbound_text(room_id, content.get("body", ""))

    async def _on_inbound_text(self, room_id: str, text: str) -> None:
        """Route a text message from Matrix to the Codex tmux pane."""
        entry = self.session_map.get_by_room(room_id)
        if not entry:
            return

        logger.info(f"Routing to Codex session {entry.session_id[:8]}: {text[:50]}...")
        success = await send_keys(entry.tmux_pane, text)

        if success:
            await self.poll_client.room_typing(
                room_id, self.config.user_id, typing=True, timeout=120000,
            )


def run_daemon():
    """Entry point for the Codex Matrix daemon."""
    from filelock import FileLock, Timeout

    config = load_config()
    if not config:
        print("No config found. Run 'ccmatrix setup' first.", file=sys.stderr)
        sys.exit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    lock = FileLock(str(STATE_DIR / "codex-daemon.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("Codex daemon is already running.", file=sys.stderr)
        sys.exit(0)

    pid_file = STATE_DIR / "codex-daemon.pid"
    pid_file.write_text(str(os.getpid()))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(STATE_DIR / "codex-daemon.log")],
    )

    daemon = CodexDaemon(config)

    def shutdown(signum, frame):
        daemon.running = False

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        asyncio.run(daemon.start())
    finally:
        pid_file.unlink(missing_ok=True)
        lock.release()


if __name__ == "__main__":
    run_daemon()
