"""Antigravity Matrix bridge — sends session messages to Matrix rooms."""

from __future__ import annotations

import logging
from pathlib import Path

from matrix_bridge.avatars import get_avatar_mxc
from matrix_bridge.config import MatrixConfig
from matrix_bridge.matrix import MatrixClient
from matrix_bridge.room_name import (
    STATUS_ACTIVE,
    STATUS_ENDED,
    build_room_name,
    detect_branch,
)
from matrix_bridge.session import SessionMap
from matrix_bridge.vm import vm_letter

from .transcript import extract_messages_after_count

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ccmatrix"


class AntigravityBridge:
    """Manages Matrix connection and message routing for Antigravity sessions."""

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.bot_client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "antigravity-sessions.json")

    async def __aenter__(self):
        await self.bot_client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.bot_client.__aexit__(*args)

    async def create_room(self, session_id: str, cwd: str) -> str | None:
        """Create a new Matrix room for an Antigravity session."""
        branch = detect_branch(cwd)
        name = build_room_name(
            cwd,
            status=STATUS_ACTIVE,
            repo_aliases=self.config.repo_aliases,
            branch=branch,
        )

        room_id = await self.bot_client.room_create(
            name=name,
            invite=[self.config.admin_user_id],
        )

        if room_id:
            # The bot is auto-joined as the room creator; the human accepts the
            # invite in their own Matrix client (or a server-side auto-join does).
            # Reuse the generic bot avatar fallback if no Antigravity-specific
            # avatar exists in the shared asset set.
            mxc = await get_avatar_mxc(self.bot_client, "antigravity", vm_letter())
            if mxc:
                await self.bot_client.room_set_avatar(room_id, mxc)
            self.session_map.set_room_id(session_id, room_id)
            self.session_map.set_last_branch(session_id, branch)
            logger.info("Created Antigravity room %s for session %s", room_id, session_id)
            return room_id

        logger.error("Failed to create room for Antigravity session %s", session_id)
        return None

    async def ensure_room(self, session_id: str, cwd: str) -> None:
        """Create or refresh the Matrix room for a session."""
        entry = self.session_map.get(session_id)
        if entry and entry.room_id:
            await self.refresh_branch_if_changed(session_id)
            return
        await self.create_room(session_id, cwd)

    async def refresh_branch_if_changed(self, session_id: str) -> None:
        """Update room name when the git branch has changed since last check."""
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id or not entry.active:
            return
        current = detect_branch(entry.cwd)
        if current == entry.last_branch:
            return
        name = build_room_name(
            entry.cwd,
            status=STATUS_ACTIVE,
            repo_aliases=self.config.repo_aliases,
            branch=current,
        )
        await self.bot_client.room_set_name(entry.room_id, name)
        self.session_map.set_last_branch(session_id, current)
        logger.info("Renamed Antigravity room for %s: branch %s → %s", session_id, entry.last_branch, current)

    async def send_messages(self, session_id: str, messages: list[dict[str, str]], notify_final: bool = False) -> int:
        """Send a batch of messages to the Matrix room for a session.

        Only assistant/tool messages are posted (via the bot client). User
        messages are not echoed — the human's own client shows them. When
        notify_final=True the last assistant message triggers a push
        notification and is tagged cc.tts for server-side TTS (if enabled).
        """
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id:
            return 0

        last_assistant_idx = None
        if notify_final:
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "assistant":
                    last_assistant_idx = i
                    break

        sent = 0
        for i, msg in enumerate(messages):
            role = msg["role"]
            text = msg["text"]
            is_final = i == last_assistant_idx

            if role == "user":
                continue
            elif role == "tool":
                await self.bot_client.room_send(entry.room_id, text[:500], catchup=True)
            else:
                await self.bot_client.room_send(
                    entry.room_id, text[:4000],
                    catchup=not is_final,
                    tts=is_final and self.config.server_side_voice,
                )
            sent += 1

        return sent

    async def catchup_from_transcript(self, session_id: str, transcript_path: Path, notify_final: bool = False) -> list[dict[str, str]]:
        """Post unseen transcript messages to Matrix and return what was sent."""
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id:
            return []

        messages, new_count = extract_messages_after_count(transcript_path, entry.synced_message_count)
        if not messages:
            self.session_map.set_synced_count(session_id, new_count)
            return []

        sent = await self.send_messages(session_id, messages, notify_final=notify_final)
        if sent:
            self.session_map.set_synced_count(session_id, new_count)
        return messages

    async def set_typing(self, session_id: str, typing: bool) -> None:
        """Set typing indicator for a session's room."""
        entry = self.session_map.get(session_id)
        if entry and entry.room_id:
            timeout = 120000 if typing else 0
            await self.bot_client.room_typing(
                entry.room_id,
                self.config.user_id,
                typing=typing,
                timeout=timeout,
            )

    async def mark_session_ended(self, session_id: str) -> None:
        """Update room name to ended status."""
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id:
            return
        name = build_room_name(
            entry.cwd,
            status=STATUS_ENDED,
            repo_aliases=self.config.repo_aliases,
            branch=entry.last_branch,
        )
        await self.bot_client.room_set_name(entry.room_id, name)
