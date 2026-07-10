"""Inbound listener daemon — receives Matrix messages and routes to Claude Code.

Runs as a background process, started by the SessionStart hook.
Long-polls /sync for messages in session rooms and injects them
into the correct tmux pane via send_keys.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from matrix_bridge.config import load_config, MatrixConfig
from matrix_bridge.matrix import MatrixClient
from matrix_bridge.session import SessionMap
from matrix_bridge.tmux import send_keys

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"


class InboundDaemon:
    """Listens for Matrix messages and routes them to Claude Code sessions."""

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "sessions.json")
        self.running = True
        self.next_batch: str | None = None

    async def start(self) -> None:
        """Start listening for Matrix messages."""
        await self._run()

    async def _run(self) -> None:
        async with self.client:
            logger.info("Daemon starting, listening in all session rooms")

            # Initial sync to skip old messages
            data = await self.client.sync(timeout=10000)
            self.next_batch = data.get("next_batch")

            # Long-poll forever
            while self.running:
                try:
                    data = await self.client.sync(
                        since=self.next_batch, timeout=30000,
                    )
                    self.next_batch = data.get("next_batch")
                    await self._process_sync(data)
                except asyncio.TimeoutError:
                    # Sync exceeded its hard timeout — connection is likely
                    # half-open. Drop the aiohttp session so the next sync
                    # opens a fresh TCP connection.
                    logger.warning("Sync timed out, reconnecting")
                    await self.client.reconnect()
                except Exception as e:
                    logger.error(f"Sync error: {type(e).__name__}: {e}")
                    await asyncio.sleep(5)

    async def _process_sync(self, data: dict) -> None:
        """Extract room messages from a sync response and route them."""
        rooms = data.get("rooms", {}).get("join", {})

        for room_id, room_data in rooms.items():
            timeline = room_data.get("timeline", {})
            for event in timeline.get("events", []):
                await self._handle_event(room_id, event)

    async def _handle_event(self, room_id: str, event: dict) -> None:
        """Handle a single timeline event."""
        # Only handle messages
        if event.get("type") != "m.room.message":
            return

        # Ignore our own messages
        if event.get("sender") == self.config.user_id:
            return

        content = event.get("content", {})
        msgtype = content.get("msgtype")

        # Skip catch-up replay messages (sent by the stop hook, not by a human)
        if content.get("cc.catchup"):
            return

        # Only text is handled. Voice arrives as ordinary @admin text via the
        # server-side voicehub STT appservice, so m.audio is ignored here.
        if msgtype == "m.text":
            await self._on_text(room_id, content.get("body", ""))

    async def _on_text(self, room_id: str, text: str) -> None:
        """Route a text message to the correct tmux pane."""
        entry = self.session_map.get_by_room(room_id)
        if not entry:
            logger.debug(f"Message in unknown room {room_id}, ignoring")
            return

        logger.info(f"Routing message to session {entry.session_id}: {text[:50]}...")
        success = await send_keys(entry.tmux_pane, text)

        if success:
            await self.client.room_typing(
                room_id, self.config.user_id, typing=True, timeout=120000,
            )
        else:
            logger.error(f"Failed to send to pane {entry.tmux_pane}")


def run_daemon():
    """Entry point for the background daemon process."""
    from filelock import FileLock, Timeout

    config = load_config()
    if not config:
        print("No config found. Run 'ccmatrix setup' first.", file=sys.stderr)
        sys.exit(1)

    # Single-instance lock — exit immediately if another daemon is running
    lock = FileLock(str(STATE_DIR / "daemon.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("Another daemon is already running.", file=sys.stderr)
        sys.exit(0)

    # Write PID file
    pid_file = STATE_DIR / "daemon.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(STATE_DIR / "daemon.log")],
    )

    daemon = InboundDaemon(config)

    # Handle graceful shutdown
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
