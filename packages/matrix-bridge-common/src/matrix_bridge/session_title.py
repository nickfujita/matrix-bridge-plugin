"""Native session titles shared by tmux and Matrix. No model calls or keystrokes."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
import hashlib
import json
import logging
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import unicodedata
from uuid import UUID

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)


def clean_title(value: str) -> str:
    """Keep titles single-line and strip terminal control characters."""
    return " ".join("".join(
        " " if unicodedata.category(c).startswith("C") else c for c in value
    ).split())[:120]


def state_dir() -> Path:
    return Path.home() / ".ccmatrix"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


@asynccontextmanager
async def room_title_lock(agent: str, session_id: str):
    """Serialize title writes from hooks, message daemons, and the title watcher."""
    root = state_dir() / "title-locks"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{agent}:{session_id}".encode()).hexdigest()
    lock = FileLock(root / f"{key}.lock", timeout=0)
    deadline = time.monotonic() + 15
    while True:
        try:
            lock.acquire()
            break
        except Timeout:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


def serialized_title(agent: str):
    def decorate(method):
        @wraps(method)
        async def wrapper(self, session_id, *args, **kwargs):
            async with room_title_lock(agent, session_id):
                return await method(self, session_id, *args, **kwargs)
        return wrapper
    return decorate


def native_title(agent: str, session_id: str) -> str | None:
    """Read native metadata. Missing titles leave the existing repo fallback intact."""
    try:
        UUID(session_id)
        if agent == "claude":
            from claude_agent_sdk import get_session_info
            info = get_session_info(session_id)
            title = (clean_title(info.custom_title or "") or None) if info else None
            request = pending_claude_title(session_id, title)
            return request["title"] if request else title
        if agent == "codex":
            return _codex_titles().get(session_id)
    except (OSError, ValueError):
        logger.debug("Cannot read %s title for %s", agent, session_id, exc_info=True)
    return None


def request_path(session_id: str) -> Path:
    UUID(session_id)
    return state_dir() / "title-requests" / f"claude-{session_id}.json"


def pending_claude_title(session_id: str, current: str | None) -> dict | None:
    path = request_path(session_id)
    try:
        request = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if current not in (request.get("title"), request.get("previous")):
        # A different native rename supersedes our pending request.
        path.unlink(missing_ok=True)
        return None
    return request if isinstance(request.get("title"), str) else None


def claude_hook(payload: dict) -> dict:
    """Let the live CLI adopt a pending rename before its next prompt/turn."""
    event = payload.get("hook_event_name")
    if event not in ("SessionStart", "UserPromptSubmit"):
        return {}
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    transcript = Path(payload.get("transcript_path") or "/")
    if not transcript.resolve().is_relative_to(root.resolve()):
        return {}  # This plugin's hook manifest is also read by Codex.
    session_id = payload.get("session_id", "")
    try:
        from claude_agent_sdk import get_session_info
        info = get_session_info(session_id)
        if info is None:
            return {}
        request = pending_claude_title(session_id, clean_title(info.custom_title or "") or None)
        if not request:
            return {}
        request_path(session_id).unlink(missing_ok=True)
        return {"hookSpecificOutput": {"hookEventName": event, "sessionTitle": request["title"]}}
    except (OSError, ValueError):
        return {}


_index_signature = None
_index_titles: dict[str, str] = {}


def _codex_titles() -> dict[str, str]:
    # One index read per change, shared by all sessions in this process. Never
    # query the transcript or the SQLite history store for every status refresh.
    global _index_signature, _index_titles
    path = codex_home() / "session_index.jsonl"
    stat = path.stat()
    signature = (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    if signature != _index_signature:
        titles = {}
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if isinstance(row, dict) and isinstance(row.get("thread_name"), str):
                        titles[row.get("id")] = clean_title(row["thread_name"])
                except (ValueError, TypeError):
                    continue  # A concurrent writer may have an incomplete last line.
        _index_titles, _index_signature = titles, signature
    return _index_titles


class CodexRPC:
    """Short-lived API connection. Does not resume or start a model turn."""

    def __enter__(self):
        # Prefer the existing daemon so its attached clients see the event.
        # Older/private TUI processes share persisted names via the standalone API.
        sock = codex_home() / "app-server-control/app-server-control.sock"
        command = ["codex", "app-server", "proxy"] if sock.exists() else [
            "codex", "app-server", "--stdio",
        ]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buffer = b""
        self.sequence = 0
        try:
            self.call("initialize", {"clientInfo": {"name": "session-title", "version": "1"}})
            self.process.stdin.write(b'{"method":"initialized"}\n')
            self.process.stdin.flush()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def call(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not select.select([self.process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                    break
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex API closed the connection")
                self.buffer += chunk
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") == self.sequence:
                if "error" in response:
                    raise RuntimeError(response["error"].get("message", "Codex API error"))
                return response.get("result", {})
        raise TimeoutError(f"Codex API timed out: {method}")

    def __exit__(self, *_):
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def rename_native(agent: str, session_id: str, title: str) -> None:
    UUID(session_id)
    if not (title := clean_title(title)):
        raise ValueError("Title must contain visible text")
    if agent == "codex":
        with CodexRPC() as rpc:
            thread = rpc.call("thread/read", {"threadId": session_id, "includeTurns": False})["thread"]
            if thread.get("source") not in ("cli", "vscode"):
                raise ValueError("Only a primary interactive session can be renamed")
            rpc.call("thread/name/set", {"threadId": session_id, "name": title})
    elif agent == "claude":
        from claude_agent_sdk import get_session_info, rename_session
        info = get_session_info(session_id)
        if info is None:
            raise ValueError("No primary Claude session found for this ID")
        rename_session(session_id, title)
        path = request_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_text(json.dumps({"title": title, "previous": info.custom_title}))
        temp.replace(path)
    else:
        raise ValueError("Agent must be codex or claude")


def session_maps():
    from .session import SessionMap
    return [(agent, SessionMap(state_dir() / filename)) for agent, filename in (
        ("codex", "codex-sessions.json"), ("claude", "sessions.json"),
    )]


def resolve_session(agent: str | None, session_id: str | None) -> tuple[str, str]:
    if session_id and agent:
        return agent, session_id
    if not agent and os.environ.get("CODEX_THREAD_ID") and os.environ.get("CLAUDE_CODE_SESSION_ID"):
        raise ValueError("Both CLI session IDs are present; pass --agent codex or --agent claude")
    if os.environ.get("CODEX_THREAD_ID") and agent in (None, "codex"):
        return "codex", session_id or os.environ["CODEX_THREAD_ID"]
    if os.environ.get("CLAUDE_CODE_SESSION_ID") and agent in (None, "claude"):
        return "claude", session_id or os.environ["CLAUDE_CODE_SESSION_ID"]
    # Claude does not export a session ID to Bash. Its SessionStart hook has
    # already bound the primary session to this pane. Never select by cwd.
    pane = os.environ.get("TMUX_PANE")
    matches = [(kind, e.session_id) for kind, smap in session_maps()
               for e in smap.active_sessions()
               if pane and e.tmux_pane == pane and (not agent or kind == agent)
               and (not session_id or e.session_id == session_id)]
    if len(matches) != 1:
        raise ValueError("Cannot identify one session; pass --agent and --session explicitly")
    return matches[0]


def tmux_title(pane: str, agent: str, session_id: str, title: str) -> bool:
    """Write plain pane options; never feed title text to a shell or tmux eval."""
    if not pane.startswith("%") or not pane[1:].isdigit():
        return False
    try:
        result = subprocess.run(["tmux", "display-message", "-p", "-t", pane,
                                 "#{pane_current_command}\t#{automatic-rename}\t#{@session_title_owner}\t#{@session_title}"],
                                capture_output=True, text=True, timeout=2)
        command, _, saved = result.stdout.rstrip("\n").partition("\t")
        if result.returncode or command != agent:
            return False
        automatic, _, saved = saved.partition("\t")
        if saved == f"{agent}:{session_id}\t{title}":
            return True
        for option, value in (("@session_title", title), ("@session_title_owner", f"{agent}:{session_id}")):
            subprocess.run(["tmux", "set-option", "-p", "-t", pane, option, value],
                           check=True, capture_output=True, timeout=2)
        # A user-option change alone does not schedule tmux's name recalculation.
        # Re-arm it only on windows that still allow automatic names.
        if automatic == "1":
            subprocess.run(["tmux", "set-option", "-w", "-t", pane, "automatic-rename", "on"],
                           check=True, capture_output=True, timeout=2)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class TitleSync:
    """Reconcile changed native names independently of message delivery."""

    def __init__(self):
        self.pending = {}
        self.panes = {}

    async def sync(self, client=None, aliases=None, *, immediate=False):
        from .room_name import build_room_name, STATUS_ACTIVE, STATUS_ENDED
        owners = {}
        updates = []
        maps = session_maps()
        # Across both harnesses, only the newest registered session owns a pane.
        for agent, smap in maps:
            for entry in smap.active_sessions():
                previous = owners.get(entry.tmux_pane)
                if not previous or entry.started_at > previous[1].started_at:
                    owners[entry.tmux_pane] = (agent, entry)
        for agent, smap in maps:
            for entry in smap.all_sessions():
                try:
                    title = await asyncio.to_thread(native_title, agent, entry.session_id)
                    owner = owners.get(entry.tmux_pane)
                    owns_pane = owner and owner[0] == agent and owner[1].session_id == entry.session_id
                    if not title:
                        if owns_pane:
                            self.panes.pop(entry.tmux_pane, None)
                            await asyncio.to_thread(clear_tmux_title, entry.tmux_pane)
                        continue
                    key = (agent, entry.session_id)
                    # Two identical observations coalesce partial/rapid metadata updates.
                    ready = immediate or self.pending.get(key) == title
                    self.pending[key] = title
                    if not ready:
                        continue
                    if owns_pane:
                        if await asyncio.to_thread(tmux_title, entry.tmux_pane, agent, entry.session_id, title):
                            self.panes[entry.tmux_pane] = (key, title)
                        else:
                            await asyncio.to_thread(clear_tmux_title, entry.tmux_pane)
                    if not client or not entry.room_id:
                        continue
                    status = STATUS_ACTIVE if entry.active else STATUS_ENDED
                    name = build_room_name(entry.cwd, status=status, repo_aliases=aliases, title=title)
                    if entry.last_room_name != name:
                        updates.append((agent, smap, entry, name))
                except Exception:
                    logger.exception("Title sync failed for %s/%s; will retry", agent, entry.session_id)
        # Clear our overrides when a session ends. The tmux format also checks
        # the foreground command, so an exited CLI cannot label a shell.
        for pane in list(self.panes):
            if pane not in owners:
                await asyncio.to_thread(clear_tmux_title, pane)
                del self.panes[pane]
        # Update every local pane before waiting on the network.
        for agent, smap, entry, name in updates:
            try:
                async with room_title_lock(agent, entry.session_id):
                    current = smap.get(entry.session_id)
                    if not current or current.active != entry.active or current.room_id != entry.room_id:
                        continue
                    if current.last_room_name == name:
                        continue
                    if await asyncio.wait_for(client.room_set_name(entry.room_id, name), timeout=10):
                        smap.set_last_room_name(entry.session_id, name)
                        smap.set_room_marked_ended(entry.session_id, not entry.active)
                        logger.info("Updated %s room title for %s", agent, entry.session_id)
            except Exception:
                logger.exception("Room title update failed for %s/%s; will retry", agent, entry.session_id)


def clear_tmux_title(pane: str):
    if not pane.startswith("%") or not pane[1:].isdigit():
        return
    for option in ("@session_title", "@session_title_owner"):
        try:
            subprocess.run(["tmux", "set-option", "-pu", "-t", pane, option],
                           capture_output=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass


async def watch():
    from .config import load_config
    from .matrix import MatrixClient
    sync = TitleSync()
    config = load_config()
    if config:
        async with MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url) as client:
            while True:
                try:
                    await sync.sync(client, config.repo_aliases)
                except Exception:
                    logger.exception("Cannot read title state; will retry")
                await asyncio.sleep(3)
    else:
        while True:
            try:
                await sync.sync()
            except Exception:
                logger.exception("Cannot read title state; will retry")
            await asyncio.sleep(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("get", "set", "watch", "claude-hook"))
    parser.add_argument("title", nargs="?")
    parser.add_argument("--agent", choices=("claude", "codex"))
    parser.add_argument("--session")
    args = parser.parse_args()
    try:
        if args.action == "claude-hook":
            payload = json.load(sys.stdin)
            output = claude_hook(payload) if not args.title or payload.get("hook_event_name") == args.title else {}
            print(json.dumps(output))
            return
        if args.action == "watch":
            state_dir().mkdir(parents=True, exist_ok=True)
            logging.basicConfig(level=logging.INFO)
            with FileLock(str(state_dir() / "session-title.lock"), timeout=0):
                asyncio.run(watch())
            return
        agent, session_id = resolve_session(args.agent, args.session)
        if args.action == "set":
            if not args.title:
                parser.error("set requires a title")
            rename_native(agent, session_id, args.title)
            # Local display updates do not depend on Matrix availability.
            asyncio.run(TitleSync().sync(immediate=True))
        print(json.dumps({"agent": agent, "session_id": session_id,
                          "title": native_title(agent, session_id)}, ensure_ascii=False))
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"session-title: {exc}\n")


if __name__ == "__main__":
    main()
