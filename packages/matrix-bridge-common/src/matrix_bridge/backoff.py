"""Circuit breaker for hook-path Matrix calls while the homeserver is down.

Hooks fire on every prompt, tool use and stop. When the homeserver is
unreachable each one would otherwise block for the full client timeout before
failing, which makes the CLI session feel broken for as long as the outage
lasts. This breaker records the failure on disk so that later hooks skip their
Matrix work immediately, then retry after an exponentially growing delay.

Skipping is safe because the transcript is the delivery queue: every hook
resumes from ``synced_message_count``, so messages missed during an outage are
replayed in order by the first hook that reaches the homeserver again.
"""

import json
import time
from pathlib import Path

INITIAL_DELAY = 30.0
MAX_DELAY = 300.0


class HomeserverUnreachable(Exception):
    """Raised instead of attempting Matrix work while the breaker is open."""

    def __init__(self, retry_in: float) -> None:
        super().__init__(f"homeserver marked unreachable; retrying in {retry_in:.0f}s")
        self.retry_in = retry_in


class HomeserverBreaker:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def retry_in(self, now: float | None = None) -> float:
        """Seconds until the next attempt is allowed; 0 when the breaker is closed."""
        now = time.time() if now is None else now
        return max(0.0, float(self._load().get("until", 0)) - now)

    def check(self, now: float | None = None) -> None:
        """Raise HomeserverUnreachable while the breaker is open."""
        remaining = self.retry_in(now)
        if remaining > 0:
            raise HomeserverUnreachable(remaining)

    def record_failure(self, now: float | None = None) -> float:
        """Open (or extend) the breaker. Returns the delay that was applied.

        Hooks run concurrently, so several may fail in the same window. Only
        the first failure after the previous window expired doubles the delay;
        the rest are the same outage observed twice and keep the current one.
        """
        now = time.time() if now is None else now
        state = self._load()
        delay = float(state.get("delay", 0))
        if float(state.get("until", 0)) > now:
            return delay
        delay = INITIAL_DELAY if delay <= 0 else min(delay * 2, MAX_DELAY)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"until": now + delay, "delay": delay}))
        return delay

    def record_success(self) -> None:
        """Close the breaker after a request reached the homeserver."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
