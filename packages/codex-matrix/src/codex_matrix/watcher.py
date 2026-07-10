"""Watch Codex session JSONL files for changes using watchdog.

When a watched session file is modified, reads new bytes from the
last-known offset, parses them, and forwards new messages to Matrix.
"""

import asyncio
import logging
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

from .transcript import CODEX_SESSIONS_DIR, extract_messages_from_offset

logger = logging.getLogger(__name__)


class SessionFileHandler(FileSystemEventHandler):
    """Handles file modification events for Codex session JSONL files."""

    def __init__(self, callback: "asyncio.coroutines", loop: asyncio.AbstractEventLoop):
        self.callback = callback
        self.loop = loop
        # Track byte offsets per file to only read new data
        self.offsets: dict[str, int] = {}

    def _process_file(self, src_path: str) -> None:
        """Read new bytes from a session file and forward messages."""
        if not src_path.endswith(".jsonl"):
            return

        path = Path(src_path)
        if not path.exists():
            return

        offset = self.offsets.get(src_path, 0)
        messages, new_offset = extract_messages_from_offset(path, offset)
        self.offsets[src_path] = new_offset

        if messages:
            asyncio.run_coroutine_threadsafe(
                self.callback(path, messages), self.loop,
            )

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):
            self._process_file(event.src_path)

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            logger.info(f"New session file detected: {Path(event.src_path).name}")
            self._process_file(event.src_path)

    def watch_file(self, path: Path) -> None:
        """Register a file to watch, initializing its offset to current size."""
        self.offsets[str(path)] = path.stat().st_size
        logger.info(f"Watching session file: {path.name} (offset: {self.offsets[str(path)]})")

    def watch_file_from_start(self, path: Path) -> None:
        """Register a file to watch from the beginning (for new sessions)."""
        self.offsets[str(path)] = 0
        logger.info(f"Watching session file from start: {path.name}")


class SessionWatcher:
    """Watches ~/.codex/sessions/ for JSONL file changes."""

    def __init__(self, on_messages):
        """on_messages: async callback(path: Path, messages: list[dict])"""
        self.on_messages = on_messages
        self.observer: Observer | None = None
        self.handler: SessionFileHandler | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start watching the sessions directory."""
        sessions_dir = CODEX_SESSIONS_DIR
        if not sessions_dir.exists():
            sessions_dir.mkdir(parents=True, exist_ok=True)

        self.handler = SessionFileHandler(self.on_messages, loop)
        self.observer = Observer()
        self.observer.schedule(self.handler, str(sessions_dir), recursive=True)
        self.observer.start()
        logger.info(f"Watching {sessions_dir} for session file changes")

    def stop(self) -> None:
        """Stop watching."""
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)

    def watch_file(self, path: Path) -> None:
        """Start watching a specific session file from current position."""
        if self.handler:
            self.handler.watch_file(path)

    def watch_file_from_start(self, path: Path) -> None:
        """Start watching a specific session file from the beginning."""
        if self.handler:
            self.handler.watch_file_from_start(path)
