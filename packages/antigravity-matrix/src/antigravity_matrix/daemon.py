"""Antigravity Matrix daemon — routes Matrix messages back into Antigravity."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from filelock import FileLock, Timeout

from matrix_bridge.config import MatrixConfig, load_config
from matrix_bridge.matrix import MatrixClient
from matrix_bridge.session import SessionMap
from matrix_bridge.tmux import send_keys

from .bridge import AntigravityBridge

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"


class AntigravityDaemon:
    """Bidirectional Matrix → Antigravity daemon.

    Outbound Antigravity → Matrix is primarily hook-driven, so this daemon only
    needs to long-poll Matrix and inject inbound user messages into tmux.
    """

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.bridge = AntigravityBridge(config)
        self.poll_client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "antigravity-sessions.json")
        self.running = True
        self.next_batch: str | None = None

    async def start(self) -> None:
        async with self.bridge:
            async with self.poll_client:
                await self._matrix_poll_loop()

    async def _matrix_poll_loop(self) -> None:
        logger.info("Starting Matrix poll loop for Antigravity inbound messages")

        data = await self.poll_client.sync(timeout=10000)
        self.next_batch = data.get("next_batch")

        while self.running:
            try:
                data = await self.poll_client.sync(since=self.next_batch, timeout=30000)
                self.next_batch = data.get("next_batch")
                await self._process_sync(data)
            except asyncio.TimeoutError:
                logger.warning("Matrix sync timed out, reconnecting")
                await self.poll_client.reconnect()
            except Exception as exc:
                logger.error("Matrix sync error: %s: %s", type(exc).__name__, exc)
                await asyncio.sleep(5)

    async def _process_sync(self, data: dict) -> None:
        rooms = data.get("rooms", {}).get("join", {})
        for room_id, room_data in rooms.items():
            timeline = room_data.get("timeline", {})
            for event in timeline.get("events", []):
                await self._handle_inbound(room_id, event)

    async def _handle_inbound(self, room_id: str, event: dict) -> None:
        if event.get("type") != "m.room.message":
            return
        if event.get("sender") == self.config.user_id:
            return

        content = event.get("content", {})
        if content.get("cc.catchup"):
            return

        # Only text is handled. Voice arrives as ordinary @admin text via the
        # server-side voicehub STT appservice, so m.audio is ignored here.
        msgtype = content.get("msgtype")
        if msgtype == "m.text":
            await self._on_inbound_text(room_id, content.get("body", ""))

    async def _on_inbound_text(self, room_id: str, text: str) -> None:
        entry = self.session_map.get_by_room(room_id)
        if not entry:
            return

        logger.info("Routing to Antigravity session %s: %s...", entry.session_id[:8], text[:50])
        success = await send_keys(entry.tmux_pane, text)
        if success:
            await self.poll_client.room_typing(room_id, self.config.user_id, typing=True, timeout=120000)


def run_daemon() -> None:
    config = load_config()
    if not config:
        print("No config found. Run 'ccmatrix setup' first.", file=sys.stderr)
        sys.exit(1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    lock = FileLock(str(STATE_DIR / "antigravity-daemon.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        print("Antigravity daemon is already running.", file=sys.stderr)
        sys.exit(0)

    pid_file = STATE_DIR / "antigravity-daemon.pid"
    pid_file.write_text(str(os.getpid()))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        handlers=[logging.FileHandler(STATE_DIR / "antigravity-daemon.log")],
    )

    daemon = AntigravityDaemon(config)

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
