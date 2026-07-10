"""Re-apply the current avatar + room-name format to every locally-tracked room.

Useful right after rolling out a new release of the bridge, to bring the
existing rooms in your Matrix client in line with the new look without
waiting for a hook to fire.

Run with:
    uv run --project packages/matrix-bridge-common python -m matrix_bridge.tools.refresh_rooms
"""

import asyncio
import logging
from pathlib import Path

from ..avatars import get_avatar_mxc
from ..config import load_config
from ..matrix import MatrixClient
from ..room_name import (
    STATUS_ACTIVE,
    STATUS_ENDED,
    build_room_name,
    detect_branch,
)
from ..session import SessionMap
from ..vm import vm_letter

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("refresh_rooms")

STATE_DIR = Path.home() / ".ccmatrix"


async def _refresh_one_map(client: MatrixClient, map_path: Path, agent: str, aliases: dict[str, str]) -> int:
    if not map_path.exists():
        return 0

    smap = SessionMap(map_path)
    letter = vm_letter()
    mxc = await get_avatar_mxc(client, agent, letter)
    if not mxc:
        logger.warning(f"No avatar generated for {agent}/{letter}; skipping {map_path.name}")
        return 0

    updated = 0
    data = smap._load()  # internal but stable enough for this one-shot tool
    for sid, raw in data.items():
        room_id = raw.get("room_id")
        if not room_id:
            continue
        cwd = raw.get("cwd", "")
        active = raw.get("active", False)
        status = STATUS_ACTIVE if active else STATUS_ENDED
        branch = detect_branch(cwd) if active else raw.get("last_branch")

        name = build_room_name(cwd, status=status, repo_aliases=aliases, branch=branch)
        await client.room_set_name(room_id, name)
        await client.room_set_avatar(room_id, mxc)
        if active:
            smap.set_last_branch(sid, branch)
        logger.info(f"  {room_id} → {name}")
        updated += 1
    return updated


async def main() -> None:
    config = load_config()
    if not config:
        raise SystemExit("No matrix bridge config found")

    async with MatrixClient(config.homeserver, config.access_token) as bot:
        logger.info("Claude Code sessions:")
        n1 = await _refresh_one_map(bot, STATE_DIR / "sessions.json", "claude", config.repo_aliases)
        logger.info(f"  ({n1} room(s) updated)\n")

        logger.info("Codex sessions:")
        n2 = await _refresh_one_map(bot, STATE_DIR / "codex-sessions.json", "codex", config.repo_aliases)
        logger.info(f"  ({n2} room(s) updated)")


if __name__ == "__main__":
    asyncio.run(main())
