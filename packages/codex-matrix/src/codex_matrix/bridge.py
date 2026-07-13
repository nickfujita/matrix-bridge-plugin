"""Codex Matrix bridge — sends session messages to Matrix rooms."""

import logging
from pathlib import Path

from matrix_bridge.avatars import get_avatar_mxc
from matrix_bridge.chunking import split_message
from matrix_bridge.config import MatrixConfig
from matrix_bridge.matrix import MatrixClient, RoomUnavailable
from matrix_bridge.room_name import (
    STATUS_ACTIVE,
    STATUS_ENDED,
    build_room_name,
    detect_branch,
)
from matrix_bridge.session import SessionMap
from matrix_bridge.vm import vm_letter

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"


class CodexBridge:
    """Manages Matrix connection and message routing for Codex sessions."""

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.bot_client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "codex-sessions.json")

    async def __aenter__(self):
        await self.bot_client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.bot_client.__aexit__(*args)

    async def create_room(self, session_id: str, cwd: str) -> str | None:
        """Create a new Matrix room for a Codex session."""
        branch = detect_branch(cwd)
        name = build_room_name(
            cwd, status=STATUS_ACTIVE,
            repo_aliases=self.config.repo_aliases, branch=branch,
        )

        room_id = await self.bot_client.room_create(
            name=name,
            invite=[self.config.admin_user_id],
        )

        if room_id:
            # The bot is auto-joined as the room creator; the human accepts the
            # invite in their own Matrix client (or a server-side auto-join does).
            mxc = await get_avatar_mxc(self.bot_client, "codex", vm_letter())
            if mxc:
                await self.bot_client.room_set_avatar(room_id, mxc)
            self.session_map.set_room_id(session_id, room_id)
            self.session_map.set_last_branch(session_id, branch)
            logger.info(f"Created Codex room {room_id} for session {session_id}")
            return room_id

        logger.error(f"Failed to create room for Codex session {session_id}")
        return None

    async def refresh_branch_if_changed(self, session_id: str) -> None:
        """Update room name when the git branch has changed since last check."""
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id or not entry.active:
            return
        current = detect_branch(entry.cwd)
        if current == entry.last_branch:
            return
        name = build_room_name(
            entry.cwd, status=STATUS_ACTIVE,
            repo_aliases=self.config.repo_aliases, branch=current,
        )
        await self.bot_client.room_set_name(entry.room_id, name)
        self.session_map.set_last_branch(session_id, current)
        logger.info(f"Renamed Codex room for {session_id}: branch {entry.last_branch} → {current}")

    async def send_messages(self, session_id: str, messages: list[dict], notify_final: bool = False) -> int:
        """Send a batch of messages to the Matrix room for a session.

        Messages have keys: role ("user", "assistant", "tool"), text.
        Only assistant/tool messages are posted (via the bot client). User
        messages are not echoed — the human's own client shows them.

        If notify_final=True, the last assistant message triggers a push
        notification (m.text) and is tagged cc.tts for server-side TTS when
        server_side_voice is enabled. Otherwise all messages are silent
        (m.notice).
        """
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id or not entry.active:
            return 0

        # Find last assistant message for notification (only when requested)
        last_assistant_idx = None
        if notify_final:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "assistant":
                    last_assistant_idx = i
                    break

        sent = 0
        healed = False
        for i, msg in enumerate(messages):
            is_final = (i == last_assistant_idx)

            if msg["role"] == "user":
                continue
            if msg["role"] == "tool":
                # Tool lines are deliberate one-liners; truncating them is fine.
                chunks, speak = [msg["text"][:500]], False
            else:
                # Long messages are split at natural boundaries rather than
                # truncated, and every chunk is tagged for TTS, so the whole reply
                # is readable and spoken. Only the last chunk notifies.
                chunks = split_message(msg["text"])
                speak = is_final and self.config.server_side_voice

            for j, chunk in enumerate(chunks):
                is_last = (j == len(chunks) - 1)
                quiet = (msg["role"] == "tool") or not (is_final and is_last)
                # If the mapped room is unreachable (deleted, or created by a
                # previous bot account), recreate it once and resend rather than
                # dropping the message silently.
                try:
                    await self.bot_client.room_send(
                        entry.room_id, chunk, catchup=quiet, tts=speak,
                        raise_on_unavailable=True,
                    )
                except RoomUnavailable as exc:
                    if healed:
                        logger.error(f"Room still unavailable after healing: {exc}")
                        return sent
                    logger.warning(f"{exc} — recreating room for session {session_id}")
                    self.session_map.set_room_id(session_id, None)
                    await self.create_room(session_id, entry.cwd)
                    entry = self.session_map.get(session_id)
                    if not entry or not entry.room_id:
                        return sent
                    healed = True
                    await self.bot_client.room_send(
                        entry.room_id, chunk, catchup=quiet, tts=speak,
                    )
            sent += 1

        return sent

    async def set_typing(self, session_id: str, typing: bool) -> None:
        """Set typing indicator for a session's room."""
        entry = self.session_map.get(session_id)
        if entry and entry.room_id and entry.active:
            timeout = 120000 if typing else 0
            await self.bot_client.room_typing(
                entry.room_id, self.config.user_id,
                typing=typing, timeout=timeout,
            )

    async def mark_session_ended(self, session_id: str) -> None:
        """Update room name to ended status."""
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id:
            return
        name = build_room_name(
            entry.cwd, status=STATUS_ENDED,
            repo_aliases=self.config.repo_aliases, branch=entry.last_branch,
        )
        await self.bot_client.room_set_name(entry.room_id, name)
