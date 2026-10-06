"""Short-lived durable Matrix /sync cursors for controlled daemon restarts."""

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)

# Resuming a token after an extended outage could route an old phone message to
# a newer terminal session. It is strictly a restart handoff aid, not a backlog
# queue. Five minutes covers a controlled upgrade while preserving old startup's
# deliberate "skip historical messages" behavior.
MAX_CURSOR_AGE_SECONDS = 5 * 60


class SyncCursor:
    """Persist the next Matrix sync token only for a brief restart handoff."""

    def __init__(self, path: Path, max_age_seconds: int = MAX_CURSOR_AGE_SECONDS):
        self.path = path
        self.max_age_seconds = max_age_seconds
        self.lock = FileLock(str(path) + ".lock")

    def load_fresh(self, *, now: float | None = None) -> str | None:
        """Return a recent cursor, never one old enough to be unsafe backlog."""
        now = time.time() if now is None else now
        try:
            raw = json.loads(self.path.read_text())
            token = raw.get("next_batch")
            recorded_at = raw.get("recorded_at")
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError):
            logger.warning("Ignoring unreadable Codex Matrix sync cursor")
            return None
        if (
            not isinstance(token, str)
            or not token
            or len(token) > 16_384
            or not isinstance(recorded_at, (int, float))
            or recorded_at > now
            or now - recorded_at > self.max_age_seconds
        ):
            return None
        return token

    def save(self, next_batch: str) -> None:
        """Atomically save a completed batch's continuation token."""
        if not isinstance(next_batch, str) or not next_batch or len(next_batch) > 16_384:
            return
        payload = json.dumps(
            {"version": 1, "next_batch": next_batch, "recorded_at": time.time()},
            separators=(",", ":"),
        )
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
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
                os.replace(temporary_path, self.path)
                temporary_path = None
        except OSError:
            logger.exception("Could not persist Codex Matrix sync cursor")
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
