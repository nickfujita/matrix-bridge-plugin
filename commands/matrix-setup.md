---
description: Configure the Matrix bridge — guided setup for a machine
allowed-tools: [Read, Write, Bash, Glob, Grep]
---

# Matrix Bridge Setup

Guided setup for the Matrix bridge. Walk through the steps below in order,
reporting progress to the user at each step. Do not hardcode any host, IP, or
token — collect every value from the user or from the environment.

## Step 0: Install dependencies

Run this FIRST — hooks will fail if deps aren't installed:
```bash
cd ${CLAUDE_PLUGIN_ROOT} && uv sync --quiet 2>&1
```
If this fails, stop and help the user fix their uv installation.

## Step 1: Collect homeserver details

Ask the user for their Matrix homeserver base URL (for example
`https://matrix.example.com`). Any Conduit-family homeserver (Conduit, conduwuit)
is tested and known to work; Synapse and Dendrite also expose the same client-server
API this bridge uses. Store it as `HOMESERVER`.

Verify it is reachable:
```bash
curl -sf "$HOMESERVER/_matrix/client/versions" >/dev/null && echo "OK" || echo "UNREACHABLE"
```

## Step 2: Machine identity (optional)

Each machine gets a per-machine avatar color + letter so you can tell rooms
apart in the room list. By default the letter is derived from the hostname (last
alphabetic character). If this machine has a random or provider-assigned
hostname (common on cloud hosts), ask the user for a single letter and set it in
their shell profile:
```bash
export CCMATRIX_VM_LETTER=A   # pick any letter; add colors in vm.py if you like
```

## Step 3: Bot login

The bridge posts as a dedicated **bot** Matrix account. Recommend one bot
account per machine (e.g. `@mybot-a`, `@mybot-b`), each with its own token.

Ask the user for the bot's username and password, then log in:
```bash
curl -sf -X POST "$HOMESERVER/_matrix/client/v3/login" \
  -H "Content-Type: application/json" \
  -d '{"type":"m.login.password","identifier":{"type":"m.id.user","user":"BOT_USERNAME"},"password":"BOT_PASSWORD"}'
```

Extract `access_token`, `user_id`, and `device_id` from the response. (If the
user already has a scoped access token, they can paste it instead of logging in.)
If login fails, help the user debug (wrong password, server unreachable, etc.).

## Step 4: Admin (human) user ID

Ask for the Matrix user ID of the human who should be invited to every session
room and chat from their phone (for example `@me:example.com`). This is
`admin_user_id`. The bridge creates each room and invites this user; they accept
the invite in their own Matrix client. No admin password or token is required —
the bridge never impersonates the human.

## Step 5: Save config

Write `~/.ccmatrix/config.json` with the collected values:
```bash
mkdir -p ~/.ccmatrix
```

Use the Write tool to create `~/.ccmatrix/config.json` (the bridge will chmod it
to `0600` on first load):
```json
{
  "homeserver": "<HOMESERVER>",
  "user_id": "<bot user_id from step 3>",
  "access_token": "<bot access_token from step 3>",
  "device_id": "<from bot login, or CCMATRIX>",
  "admin_user_id": "<human user ID from step 4>",
  "server_side_voice": true
}
```

Optional keys:
- `proxy_url` — route every Matrix HTTP call through a local forward proxy
  (e.g. `"http://127.0.0.1:1055"`); omit for a direct connection.
- `repo_aliases` — map long repo names to friendly room labels, e.g.
  `{"my-really-long-repo-name": "myrepo"}`.
- `server_side_voice` — leave `true` to tag final messages `cc.tts` for the
  optional server-side voicehub; set `false` as a kill switch.

## Step 6: Test connection

Create a test room to verify everything works (replace the bearer token and
invite target with the values from above):
```bash
curl -sf -X POST "$HOMESERVER/_matrix/client/v3/createRoom" \
  -H "Authorization: Bearer $BOT_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"visibility":"private","preset":"private_chat","name":"matrix bridge setup test","invite":["ADMIN_USER_ID"]}'
```

If this succeeds, the bridge is properly configured. Report success.

## Step 7: Enable and start

Enable the bridge and start the daemon to verify it can connect:
```bash
touch ~/.ccmatrix/enabled
uv run --project ${CLAUDE_PLUGIN_ROOT} python -m claude_code_matrix.daemon &
DAEMON_PID=$!
sleep 2
kill -0 $DAEMON_PID 2>/dev/null && echo "Daemon started successfully (PID $DAEMON_PID)" || echo "Daemon failed to start"
```

If the daemon fails, check `~/.ccmatrix/daemon.log` and help the user debug.

To also wire up the Codex and Antigravity bridges on this machine:
```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} codex-matrix enable
uv run --project ${CLAUDE_PLUGIN_ROOT} antigravity-matrix enable
```

## Optional: server-side voice

Voice (spoken replies + voice-note transcription) is **not** part of this
plugin. It is provided by two optional companion services:

- **matrix-voicehub** — a Matrix appservice that watches for `cc.tts`-tagged
  messages and posts synthesized audio (and transcribes inbound voice notes).
- **voice-server** — an OpenAI-compatible TTS/STT HTTP service that voicehub
  calls to do the actual synthesis/transcription.

Deploy those on your homeserver if you want voice; the plugin needs no local
voice setup beyond leaving `server_side_voice` set to `true`.

## Summary

After all steps, present a summary:
- Homeserver connection: OK/FAIL
- Bot user: (user_id)
- Admin (human) user: (user_id)
- Server-side voice tagging: on/off
- Bridge: enabled
- Daemon: running (PID)

Tell the user: "Setup complete! Open any Matrix client and you should see a test
room. Your sessions will now sync to Matrix."
