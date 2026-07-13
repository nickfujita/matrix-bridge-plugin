"""Thin Matrix client-server API wrapper using aiohttp.

Replaces matrix-nio with direct HTTP calls, giving full control
over impersonation (sending messages as other users via access tokens).
"""

import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)


class RoomUnavailable(Exception):
    """A room the session is mapped to is gone, or this bot is not a member.

    Raised by send paths so callers can recreate the room instead of dropping
    messages. Happens when the homeserver loses a room, or when the bot account
    changes (a room created by a previous account is unreachable by the new one).
    """

    def __init__(self, room_id: str, detail: str = "") -> None:
        super().__init__(f"room {room_id} unavailable: {detail}")
        self.room_id = room_id

# Global transaction counter for Matrix event dedup
_txn_counter = int(time.time() * 1000)


def _next_txn_id() -> str:
    global _txn_counter
    _txn_counter += 1
    return str(_txn_counter)


def _tables_to_lists(text: str) -> str:
    """Convert markdown tables to definition-style lists for mobile readability.

    | Name | Status | Notes |        **Name**: Status — Notes
    |------|--------|-------|   →    **Name2**: Status2 — Notes2
    | Name2| Status2| Notes2|
    """
    import re

    lines = text.split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Detect table: current line has pipes and next line is a separator (|---|---|)
        if (
            "|" in line
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s:*-]+\|", lines[i + 1])
        ):
            # Parse headers
            headers = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # skip header + separator

            # Parse data rows
            while i < len(lines) and "|" in lines[i] and not re.match(r"^\s*\|[\s:-]+\|$", lines[i]):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                # Use first column as bold label, rest as values
                if len(cells) >= 2 and len(headers) >= 2:
                    label = cells[0]
                    parts = []
                    for j in range(1, len(cells)):
                        if j < len(headers) and cells[j]:
                            parts.append(f"{headers[j]}: {cells[j]}")
                        elif cells[j]:
                            parts.append(cells[j])
                    result.append(f"**{label}**: {' — '.join(parts)}")
                elif cells:
                    result.append(" — ".join(cells))
                i += 1
            result.append("")  # blank line after table
        else:
            result.append(line)
            i += 1

    return "\n".join(result)


class MatrixClient:
    """Lightweight Matrix client using aiohttp."""

    # Default timeout for normal API calls (connect + read).
    # 60s accommodates large media uploads/downloads over the local bridge.
    DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60)

    def __init__(self, homeserver: str, access_token: str, proxy: str | None = None):
        self.homeserver = homeserver.rstrip("/")
        self.access_token = access_token
        # Outbound HTTP proxy applied to every request. None = direct connection.
        self.proxy = proxy or None
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=self.DEFAULT_TIMEOUT,
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
            self.session = None

    async def reconnect(self) -> None:
        """Drop the current aiohttp session and create a fresh one.

        Why: aiohttp's per-request ClientTimeout has been observed to not fire
        when the underlying TCP socket goes half-open (server restart, NAT
        eviction). The connection pool keeps handing out the dead socket. A
        full session swap forces a new connector and a new TCP handshake.
        """
        if self.session:
            await self.session.close()
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=self.DEFAULT_TIMEOUT,
        )

    def _url(self, path: str) -> str:
        return f"{self.homeserver}{path}"

    async def room_create(
        self,
        name: str,
        invite: list[str] | None = None,
        preset: str = "private_chat",
    ) -> str | None:
        """Create a private room. Returns room_id or None."""
        body = {
            "visibility": "private",
            "preset": preset,
            "name": name,
            "creation_content": {"m.federate": False},
        }
        if invite:
            body["invite"] = invite

        async with self.session.post(
            self._url("/_matrix/client/v3/createRoom"), json=body, proxy=self.proxy,
        ) as resp:
            data = await resp.json()
            if resp.status == 200:
                return data["room_id"]
            logger.error(f"room_create failed: {data}")
            return None

    async def room_join(self, room_id: str) -> bool:
        """Join a room (accept an invite)."""
        async with self.session.post(
            self._url(f"/_matrix/client/v3/join/{room_id}"), json={}, proxy=self.proxy,
        ) as resp:
            if resp.status == 200:
                return True
            data = await resp.json()
            logger.error(f"room_join failed: {data}")
            return False

    async def room_send(
        self, room_id: str, body: str, catchup: bool = False, tts: bool = False,
        raise_on_unavailable: bool = False,
    ) -> str | None:
        """Send a text message. Returns event_id or None.

        If catchup=True, adds cc.catchup field so the daemon ignores it.
        If tts=True, adds cc.tts so the server-side voicehub synthesizes it.

        If raise_on_unavailable=True, a definitive "this room is not usable by
        this account" response (403/404) raises RoomUnavailable so the caller can
        recreate the room. Transient failures still return None. Callers that
        merely decorate a room (tool one-liners, typing) leave this False and keep
        the old best-effort behavior.
        """
        import markdown as md

        txn_id = _next_txn_id()
        # Catchup messages use m.notice (suppressed by push rules, renders dimmed)
        msgtype = "m.notice" if catchup else "m.text"
        plain = _tables_to_lists(body[:50000])
        content = {"msgtype": msgtype, "body": plain}

        # Convert markdown to HTML for rich rendering in clients
        html = md.markdown(plain, extensions=["fenced_code"])
        if html != plain:
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = html

        if catchup:
            content["cc.catchup"] = True
        if tts:
            content["cc.tts"] = True

        async with self.session.put(
            self._url(f"/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{txn_id}"),
            json=content, proxy=self.proxy,
        ) as resp:
            data = await resp.json()
            if resp.status == 200:
                return data.get("event_id")
            logger.error(f"room_send failed ({resp.status}): {data}")
            if raise_on_unavailable and (
                resp.status in (403, 404)
                or data.get("errcode") in ("M_FORBIDDEN", "M_NOT_FOUND", "M_UNKNOWN_ROOM")
            ):
                # The room is gone, or this account was never a member of it
                # (e.g. it was created by a previous bot account). Signal the
                # caller so it can recreate the room instead of silently
                # dropping messages forever.
                raise RoomUnavailable(room_id, str(data))
            return None

    async def room_set_name(self, room_id: str, name: str) -> bool:
        """Update room name via state event."""
        async with self.session.put(
            self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.name"),
            json={"name": name}, proxy=self.proxy,
        ) as resp:
            if resp.status == 200:
                return True
            data = await resp.json()
            logger.error(f"room_set_name failed: {data}")
            return False

    async def room_set_avatar(self, room_id: str, mxc_url: str) -> bool:
        """Set room avatar via state event."""
        async with self.session.put(
            self._url(f"/_matrix/client/v3/rooms/{room_id}/state/m.room.avatar"),
            json={"url": mxc_url}, proxy=self.proxy,
        ) as resp:
            if resp.status == 200:
                return True
            data = await resp.json()
            logger.error(f"room_set_avatar failed: {data}")
            return False

    async def room_typing(self, room_id: str, user_id: str, typing: bool, timeout: int = 30000) -> None:
        """Set typing indicator."""
        async with self.session.put(
            self._url(f"/_matrix/client/v3/rooms/{room_id}/typing/{user_id}"),
            json={"typing": typing, "timeout": timeout}, proxy=self.proxy,
        ) as resp:
            if resp.status != 200:
                pass  # Typing failures are non-critical

    async def sync(self, since: str | None = None, timeout: int = 30000) -> dict:
        """Long-poll /sync. Returns the full sync response.

        Wrapped in asyncio.wait_for as a hard cap. aiohttp's ClientTimeout has
        been observed to not fire on stale TCP connections, leaving the
        long-poll hung forever; asyncio cancellation always fires.
        """
        params = {"timeout": str(timeout)}
        if since:
            params["since"] = since

        sync_timeout_sec = timeout / 1000 + 30

        async def _do_sync() -> dict:
            async with self.session.get(
                self._url("/_matrix/client/v3/sync"), params=params, proxy=self.proxy,
                timeout=aiohttp.ClientTimeout(total=sync_timeout_sec),
            ) as resp:
                return await resp.json()

        return await asyncio.wait_for(_do_sync(), timeout=sync_timeout_sec + 5)

    async def upload(self, data: bytes, content_type: str, filename: str) -> str | None:
        """Upload media. Returns mxc:// URI or None."""
        async with self.session.post(
            self._url("/_matrix/media/v3/upload"),
            data=data,
            headers={
                "Content-Type": content_type,
                "Authorization": f"Bearer {self.access_token}",
            },
            params={"filename": filename},
            proxy=self.proxy,
        ) as resp:
            result = await resp.json()
            if resp.status == 200:
                return result.get("content_uri")
            logger.error(f"upload failed: {result}")
            return None

    async def download(self, mxc_url: str) -> bytes | None:
        """Download media from mxc:// URL. Returns bytes or None."""
        # mxc://server/media_id → /_matrix/client/v1/media/download/server/media_id
        if not mxc_url.startswith("mxc://"):
            return None
        path = mxc_url[6:]  # strip mxc://

        async with self.session.get(
            self._url(f"/_matrix/client/v1/media/download/{path}"), proxy=self.proxy,
        ) as resp:
            if resp.status == 200:
                return await resp.read()
            logger.error(f"download failed: {resp.status}")
            return None
