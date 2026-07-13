"""Matrix bridge — sends and receives messages via direct HTTP API."""

import logging
from pathlib import Path

from filelock import FileLock

from matrix_bridge.avatars import get_avatar_mxc
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

class MatrixBridge:
    """Manages Matrix connection and message routing."""

    def __init__(self, config: MatrixConfig):
        self.config = config
        self.bot_client = MatrixClient(config.homeserver, config.access_token, proxy=config.proxy_url)
        self.session_map = SessionMap(STATE_DIR / "sessions.json")

    async def __aenter__(self):
        await self.bot_client.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.bot_client.__aexit__(*args)

    async def create_room(self, session_id: str, cwd: str) -> str | None:
        """Create a new Matrix room for a session. Returns room ID."""
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
            mxc = await get_avatar_mxc(self.bot_client, "claude", vm_letter())
            if mxc:
                await self.bot_client.room_set_avatar(room_id, mxc)
            self.session_map.set_room_id(session_id, room_id)
            self.session_map.set_last_branch(session_id, branch)
            logger.info(f"Created room {room_id} for session {session_id}")
            return room_id

        logger.error(f"Failed to create room for session {session_id}")
        return None

    async def update_room_status(self, session_id: str, status: str) -> None:
        """Update the room name to reflect current status."""
        entry = self.session_map.get(session_id)
        if not entry or not entry.room_id:
            return

        branch = detect_branch(entry.cwd) if status == STATUS_ACTIVE else entry.last_branch
        name = build_room_name(
            entry.cwd, status=status,
            repo_aliases=self.config.repo_aliases, branch=branch,
        )
        await self.bot_client.room_set_name(entry.room_id, name)
        if status == STATUS_ACTIVE:
            self.session_map.set_last_branch(session_id, branch)

    async def refresh_branch_if_changed(self, session_id: str) -> None:
        """Update the room name when the git branch has changed since last check."""
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
        logger.info(f"Renamed room for {session_id}: branch {entry.last_branch} → {current}")

    async def catchup_from_transcript(self, session_id: str, notify_final: bool = False) -> int:
        """Post unseen transcript messages to the Matrix room.

        Only Claude (assistant/tool) messages are posted, via the bot client.
        User messages are not echoed — the human's own client shows them.
        Uses a file lock to prevent concurrent hook invocations from
        sending duplicates (the stop hook can fire multiple times per turn).

        If notify_final=True, the last assistant message in the batch is sent
        as m.text (triggers push notification) instead of m.notice, and is
        tagged cc.tts for server-side TTS when server_side_voice is enabled.
        Returns the number of new messages synced.
        """
        from .transcript import find_transcript, extract_messages

        # Lock to prevent concurrent catch-ups from racing on synced_message_count
        lock = FileLock(str(STATE_DIR / "catchup.lock"), timeout=30)
        with lock:
            entry = self.session_map.get(session_id)
            if not entry:
                return 0

            transcript = find_transcript(session_id)
            if not transcript:
                logger.warning(f"No transcript found for session {session_id}")
                return 0

            all_messages = extract_messages(transcript)
            already_synced = entry.synced_message_count

            new_messages = all_messages[already_synced:]
            if not new_messages:
                return 0

            # Ensure room exists
            if not entry.room_id:
                await self.create_room(session_id, entry.cwd)
                entry = self.session_map.get(session_id)
                if not entry or not entry.room_id:
                    return 0
            else:
                # Cheap branch-change check so on-the-fly renames track the working tree.
                await self.refresh_branch_if_changed(session_id)

            # Find index of last assistant message for notification
            last_assistant_idx = None
            if notify_final:
                for i in range(len(new_messages) - 1, -1, -1):
                    if new_messages[i]["role"] == "assistant":
                        last_assistant_idx = i
                        break

            # Send each Claude message via the bot client. User messages are
            # not echoed — the human's own client already shows them.
            #
            # If the mapped room turns out to be unreachable (deleted, or created
            # by a previous bot account), recreate it once and resend into the new
            # room. Never advance synced_message_count past messages that were not
            # actually delivered — doing so drops them permanently.
            healed = False
            for i, msg in enumerate(new_messages):
                is_final = (i == last_assistant_idx)
                if msg["role"] == "user":
                    continue
                text = msg["text"][:4000] if len(msg["text"]) > 4000 else msg["text"]
                # Final assistant message: m.text (notifies), others: m.notice (silent).
                # Tag the final message cc.tts so the server-side voicehub speaks it.
                try:
                    await self.bot_client.room_send(
                        entry.room_id, text,
                        catchup=not is_final,
                        tts=is_final and self.config.server_side_voice,
                        raise_on_unavailable=True,
                    )
                except RoomUnavailable as exc:
                    if healed:
                        logger.error(f"Room still unavailable after healing: {exc}")
                        return 0
                    logger.warning(f"{exc} — recreating room for session {session_id}")
                    self.session_map.set_room_id(session_id, None)
                    await self.create_room(session_id, entry.cwd)
                    entry = self.session_map.get(session_id)
                    if not entry or not entry.room_id:
                        return 0
                    healed = True
                    await self.bot_client.room_send(
                        entry.room_id, text,
                        catchup=not is_final,
                        tts=is_final and self.config.server_side_voice,
                    )

            # Update count inside the lock so the next caller sees it
            total = already_synced + len(new_messages)
            self.session_map.set_synced_count(session_id, total)
            return len(new_messages)

    async def mark_session_ended(self, session_id: str) -> None:
        """Update room name to ended status."""
        await self.update_room_status(session_id, STATUS_ENDED)
