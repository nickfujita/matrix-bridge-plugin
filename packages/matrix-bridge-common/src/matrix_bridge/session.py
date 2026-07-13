"""Session-to-tmux mapping and room tracking.

Maintains a JSON file mapping Claude Code session IDs to tmux panes
and Matrix room IDs, enabling bidirectional routing.
"""

import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from filelock import FileLock


@dataclass
class SessionEntry:
    session_id: str
    tmux_pane: str  # e.g. "%5"
    cwd: str
    room_id: str | None = None  # Matrix room ID for this session
    active: bool = True
    started_at: float = 0.0
    ended_at: float | None = None
    synced_message_count: int = 0  # How many transcript messages have been sent to Matrix
    pending_user_skips: int = 0  # User messages the daemon already sent/echoed to room
    last_branch: str | None = None  # Last git branch the room name reflects


class SessionMap:
    """Thread-safe session↔tmux↔Matrix mapping backed by a JSON file."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = FileLock(str(path) + ".lock")

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        # Migrate old thread_id keys to room_id
        for entry in data.values():
            if "thread_id" in entry:
                entry.pop("thread_id")
        return data

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def register(self, session_id: str, tmux_pane: str, cwd: str) -> None:
        with self.lock:
            data = self._load()
            existing = data.get(session_id)
            pane_is_known = bool(tmux_pane and tmux_pane != "unknown")

            # Only one live session can own a given tmux pane. Retire any
            # older sessions still marked active on that pane so stale Matrix
            # rooms do not route into a newer Codex/Claude session.
            if pane_is_known:
                now = time.time()
                for other_session_id, other in data.items():
                    if other_session_id == session_id:
                        continue
                    if other.get("active") and other.get("tmux_pane") == tmux_pane:
                        other["active"] = False
                        other["ended_at"] = now

            if existing:
                # Resume — update mutable fields, preserve room_id and history.
                # Ignore placeholder pane values so a watcher-created provisional
                # entry does not overwrite a real tmux binding.
                if pane_is_known:
                    existing["tmux_pane"] = tmux_pane
                existing["cwd"] = cwd
                existing["active"] = True
                existing["ended_at"] = None
            else:
                data[session_id] = asdict(SessionEntry(
                    session_id=session_id,
                    tmux_pane=tmux_pane or "unknown",
                    cwd=cwd,
                    started_at=time.time(),
                ))
            self._save(data)

    def deregister(self, session_id: str) -> None:
        with self.lock:
            data = self._load()
            if session_id in data:
                data[session_id]["active"] = False
                data[session_id]["ended_at"] = time.time()
                self._save(data)

    def set_room_id(self, session_id: str, room_id: str | None) -> None:
        """Set (or clear, with None) the room mapped to a session."""
        with self.lock:
            data = self._load()
            if session_id in data:
                data[session_id]["room_id"] = room_id
                self._save(data)

    def set_last_branch(self, session_id: str, branch: str | None) -> None:
        with self.lock:
            data = self._load()
            if session_id in data:
                data[session_id]["last_branch"] = branch
                self._save(data)

    def set_synced_count(self, session_id: str, count: int) -> None:
        with self.lock:
            data = self._load()
            if session_id in data:
                data[session_id]["synced_message_count"] = count
                self._save(data)

    def increment_user_skips(self, session_id: str) -> None:
        """Mark that the daemon already sent/echoed a user message to the room."""
        with self.lock:
            data = self._load()
            if session_id in data:
                data[session_id]["pending_user_skips"] = data[session_id].get("pending_user_skips", 0) + 1
                self._save(data)

    def consume_user_skip(self, session_id: str) -> bool:
        """Check and consume one pending skip. Returns True if a skip was available."""
        with self.lock:
            data = self._load()
            if session_id in data and data[session_id].get("pending_user_skips", 0) > 0:
                data[session_id]["pending_user_skips"] -= 1
                self._save(data)
                return True
            return False

    def _entry_from_dict(self, raw: dict) -> SessionEntry:
        # Strip any keys we don't recognize so format evolution is forward-safe.
        known = {f.name for f in SessionEntry.__dataclass_fields__.values()}
        return SessionEntry(**{k: v for k, v in raw.items() if k in known})

    def get(self, session_id: str) -> SessionEntry | None:
        data = self._load()
        if session_id not in data:
            return None
        return self._entry_from_dict(data[session_id])

    def get_by_room(self, room_id: str) -> SessionEntry | None:
        data = self._load()
        for entry in data.values():
            if entry.get("room_id") == room_id and entry.get("active"):
                return self._entry_from_dict(entry)
        return None

    def active_sessions(self) -> list[SessionEntry]:
        data = self._load()
        return [
            self._entry_from_dict(entry)
            for entry in data.values()
            if entry.get("active")
        ]
