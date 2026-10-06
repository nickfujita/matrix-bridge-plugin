"""Durable, bounded receipts for Matrix events injected into Codex.

Matrix /sync is at-least-once at the client boundary: reconnects and homeserver
retries can expose an event again.  Tmux input is not idempotent, so retain only
Matrix event IDs (never message bodies) after a successful injection.
"""

import json
import logging
import os
import tempfile
from collections import deque
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)

DEFAULT_RECEIPT_LIMIT = 2_048


class InboundEventReceipts:
    """Remember recently injected Matrix event IDs across daemon restarts.

    The single Codex daemon normally owns this file.  A file lock and atomic
    replacement also make a concurrent lifecycle handoff safe: neither process
    can leave a partial JSON document behind.
    """

    def __init__(self, path: Path, limit: int = DEFAULT_RECEIPT_LIMIT):
        if limit < 1:
            raise ValueError("limit must be positive")
        self.path = path
        self.limit = limit
        self.lock = FileLock(str(path) + ".lock")
        self._event_ids = deque(self._load(), maxlen=limit)

    def contains(self, event_id: str) -> bool:
        """Return whether this Matrix event was already injected."""
        return isinstance(event_id, str) and event_id in self._event_ids

    def record(self, event_id: str) -> None:
        """Persist a receipt after tmux accepted the event's input.

        ``send_keys`` is awaited by the caller before this method runs.  That
        keeps a failed tmux injection retryable while preventing repeated /sync
        deliveries from becoming repeated terminal prompts.
        """
        if not isinstance(event_id, str) or not event_id or len(event_id) > 512:
            return

        # Keep the in-process fence even if persistence unexpectedly fails.  A
        # restart after a storage failure cannot be made exactly-once because
        # tmux offers no input receipt; the error remains visible in the daemon
        # log rather than silently pretending durable state was written.
        if event_id in self._event_ids:
            self._event_ids.remove(event_id)
        self._event_ids.append(event_id)

        try:
            with self.lock:
                disk_ids = deque(self._load(), maxlen=self.limit)
                if event_id in disk_ids:
                    disk_ids.remove(event_id)
                disk_ids.append(event_id)
                self._save(disk_ids)
                self._event_ids = disk_ids
        except OSError:
            logger.exception("Could not persist Codex inbound Matrix event receipt")

    def count(self) -> int:
        """Return the number of retained receipts for status/diagnostics."""
        return len(self._event_ids)

    def _load(self) -> list[str]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable Codex inbound Matrix receipt state")
            return []

        values = raw.get("event_ids") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            logger.warning("Ignoring malformed Codex inbound Matrix receipt state")
            return []

        # Event IDs are opaque Matrix identifiers, never content.  Bound each
        # item too, so a corrupted file cannot consume unbounded memory.
        return [value for value in values if isinstance(value, str) and 0 < len(value) <= 512][-self.limit:]

    def _save(self, event_ids: deque[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "event_ids": list(event_ids)}, separators=(",", ":"))
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            os.replace(temporary_path, self.path)
        finally:
            # os.replace removes it on the usual path; this only covers a
            # failed replace without using a destructive directory cleanup.
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
