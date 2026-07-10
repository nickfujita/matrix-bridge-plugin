---
description: Show Matrix bridge status — daemon, sessions, voice, connection
allowed-tools: [Read, Bash, Glob]
---

# Matrix Bridge Status

Show the user the current state of their Matrix bridge. Check and report ALL of:

1. **Config**: Read `~/.ccmatrix/config.json` — show homeserver, bot user, admin
   user ID, and whether `server_side_voice` is enabled. If missing, tell the
   user to run `/matrix-setup`.

2. **Enabled**: Check if `~/.ccmatrix/enabled` exists.

3. **Daemon**: Check if the inbound listener is running:
   ```bash
   PID=$(cat ~/.ccmatrix/daemon.pid 2>/dev/null) && kill -0 $PID 2>/dev/null && echo "running (PID $PID)" || echo "not running"
   ```

4. **Voice**: Voice (TTS out / STT in) is handled server-side by the optional
   `matrix-voicehub` appservice, not by this plugin. Report whether
   `server_side_voice` is `true` in the config (the plugin tags final messages
   with `cc.tts` when it is). If voicehub is not deployed, no audio is produced —
   that is expected and voice is optional.

5. **Active sessions**: Read `~/.ccmatrix/sessions.json` and list active sessions
   with their project name, tmux pane, and room ID.

6. **Connection test**: Verify the Matrix server is reachable:
   ```bash
   curl -sf "$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.ccmatrix/config.json")))["homeserver"])')/_matrix/client/versions" >/dev/null && echo "OK" || echo "FAILED"
   ```

Present this as a clean summary.
