---
description: Enable Matrix bridge — start forwarding messages to your phone
allowed-tools: [Bash, Read]
---

Enable the Matrix bridge so Claude Code messages are forwarded to Matrix.

**Step 1: Check config exists**

Read `~/.ccmatrix/config.json`. If it doesn't exist, tell the user to run `/matrix-setup` first and stop.

**Step 2: Enable the bridge**

```bash
mkdir -p ~/.ccmatrix && touch ~/.ccmatrix/enabled
```

**Step 3: Enable the Codex bridge wiring**

Install or refresh the Codex notify wrapper and start the Codex Matrix daemon:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} codex-matrix enable
```

**Step 4: Catch up the current session**

Sync any existing conversation history to a new Matrix room:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} python -c "
import asyncio, os
from claude_code_matrix.config import load_config
from claude_code_matrix.bridge import MatrixBridge
from claude_code_matrix.session import SessionMap
from pathlib import Path

async def catchup():
    config = load_config()
    if not config:
        print('No Matrix config found. Run /matrix-setup first.')
        return

    session_id = os.environ.get('CLAUDE_SESSION_ID', '')
    if not session_id:
        print('No active session detected.')
        return

    state_dir = Path.home() / '.ccmatrix'
    session_map = SessionMap(state_dir / 'sessions.json')
    entry = session_map.get(session_id)

    async with MatrixBridge(config) as bridge:
        if not entry or not entry.room_id:
            cwd = os.getcwd()
            project_name = Path(cwd).name
            await bridge.create_room(session_id, project_name)

        count = await bridge.catchup_from_transcript(session_id)
        if count > 0:
            print(f'Synced {count} messages to Matrix room.')
        else:
            print('Room is up to date.')

asyncio.run(catchup())
"
```

Report the result to the user: how many messages were synced, and confirm the bridge is now active.
