# matrix-bridge-plugin

A self-hosted **Matrix bridge for AI coding CLIs**. It mirrors each coding
session into a room on *your own* Matrix homeserver, so you can watch progress
and chat back — from your phone, a tablet, or any Matrix client — while the
agent keeps working on your machine.

One plugin, three framework variants in this repo:

- **claude-code-matrix** — for [Claude Code](https://claude.com/claude-code)
- **codex-matrix** — for Codex CLI
- **antigravity-matrix** — for Google Antigravity CLI

They share a common core (`matrix-bridge-common`) and behave the same way:
one Matrix room per session, named `repo/branch`, with a per-machine colored
avatar so you can tell rooms apart at a glance.

## What it does

- **Session rooms.** Each coding session gets its own Matrix room, named after
  the repo and branch. Assistant messages, tool activity, and completion
  notifications are forwarded to the room in near real time.
- **Chat from your phone.** Reply in the room from any Matrix client. Your reply
  is injected into the running CLI via **tmux**, exactly as if you had typed it
  at the terminal — so you can steer or answer prompts without being at your
  desk.
- **Per-machine identity.** Every machine gets a distinct avatar color + letter
  and a framework badge, so a room list spanning several machines stays
  readable.
- **Optional voice.** When enabled, the plugin tags the final assistant message
  of each turn with `cc.tts`. A separate server-side service (see below) turns
  that into spoken audio in the room, and transcribes voice notes you send back.
  The plugin itself does **no** audio processing.

## Architecture

```
  ┌─────────────────────────────┐         ┌──────────────────────┐
  │  your machine                │         │  Matrix homeserver   │
  │                              │         │  (Conduit family)    │
  │  Claude Code / Codex /       │  events │                      │
  │  Antigravity                 ├────────►│   session room       │
  │    │  hooks + daemon         │         │   (repo/branch)      │
  │    │                         │◄────────┤                      │
  │    ▼  tmux inject            │ replies │                      │
  │  matrix-bridge-plugin        │         └───────┬──────────────┘
  └─────────────────────────────┘                 │ cc.tts tag
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │  matrix-voicehub (optional)   │
                                    │  Matrix appservice: TTS out,  │
                                    │  STT in                       │
                                    └───────────────┬───────────────┘
                                                    │ HTTP
                                                    ▼
                                    ┌──────────────────────────────┐
                                    │  voice-server (optional)      │
                                    │  OpenAI-compatible TTS/STT    │
                                    └──────────────────────────────┘
```

Outbound: hooks in each CLI push session events to a small daemon, which posts
them to the room. Inbound: the daemon watches the room and injects your replies
into the CLI's tmux pane.

### The three companion repos

Voice is fully optional and lives in two separate projects, so you can run the
text bridge alone or add spoken voice later:

| Repo | Role |
|------|------|
| **matrix-bridge-plugin** (this repo) | The CLI plugin. Session rooms, tmux injection, and `cc.tts` tagging. Runs on your machine(s). |
| **matrix-voicehub** | Optional Matrix **appservice**. Watches for the plugin's `cc.tts`-tagged messages and posts synthesized audio; transcribes inbound voice notes and re-posts them as text. Runs on/near your homeserver. |
| **voice-server** | Optional **OpenAI-compatible TTS/STT HTTP service** that voicehub calls to do the actual synthesis and transcription. |

If you don't deploy voicehub + voice-server, everything text still works; the
`cc.tts` tag is simply ignored.

## Install

This is a Claude Code plugin distributed via a plugin marketplace.

```
/plugin marketplace add nickfujita/matrix-bridge-plugin
/plugin install claude-code-matrix
```

Then configure it:

```
/matrix-setup
```

`/matrix-setup` walks you through pointing the bridge at your homeserver, a bot
account, and the human account to invite. To also wire up the Codex and
Antigravity variants on the same machine:

```bash
uv run --project ${CLAUDE_PLUGIN_ROOT} codex-matrix enable
uv run --project ${CLAUDE_PLUGIN_ROOT} antigravity-matrix enable
```

Runtime requirements: `uv` (for the Python packages), `tmux` (for reply
injection), and `ffmpeg` if you use the optional voice path.

Slash commands: `/matrix-setup`, `/matrix-status`, `/matrix-enable`,
`/matrix-disable`.

## Configuration

Config lives in `~/.ccmatrix/config.json` (written `0600`). Every key can also
be supplied via an environment variable, which takes precedence over the file.

| Config key | Env var | Default | Meaning |
|------------|---------|---------|---------|
| `homeserver` | `CCMATRIX_HOMESERVER` | — (required) | Homeserver base URL, e.g. `https://matrix.example.com`. |
| `user_id` | `CCMATRIX_USER_ID` | — (required) | Bot account MXID, e.g. `@mybot-a:example.com`. |
| `access_token` | `CCMATRIX_ACCESS_TOKEN` | — (required) | The bot's scoped access token. |
| `admin_user_id` | `CCMATRIX_ADMIN_USER_ID` | — (required) | The human MXID invited to every session room. |
| `device_id` | `CCMATRIX_DEVICE_ID` | `CCMATRIX` | Matrix device ID for the bot session. |
| `server_side_voice` | `CCMATRIX_SERVER_SIDE_VOICE` | `true` | Tag the final message of each turn `cc.tts` for the optional voicehub. Set `false` as a kill switch (no local synthesis is involved either way). |
| `proxy_url` | `CCMATRIX_PROXY_URL` | `""` | Route every Matrix HTTP call through a forward proxy, e.g. `http://127.0.0.1:1055`. Blank = direct connection. |
| `repo_aliases` | — | `{}` | Map long repo names to friendly room labels, e.g. `{"my-really-long-repo-name": "myrepo"}`. |

Additional environment-only settings:

| Env var | Meaning |
|---------|---------|
| `CCMATRIX_VM_LETTER` | Force this machine's identity letter (color + avatar). Required on hosts whose hostname isn't distinctive (many cloud instances); otherwise derived from the hostname's last alphabetic character. |
| `CCMATRIX_ANTIGRAVITY_SKILL_DIRS` | (Antigravity only) `os.pathsep`-separated list of local skill roots to expose to Antigravity. Defaults to `~/.agents/skills`. |

See [docs/multi-machine-deployment.md](docs/multi-machine-deployment.md) for
running the bridge across several machines (per-machine bot accounts, identity
letters/colors, and the `refresh_rooms` tool).

## Homeserver requirements

Tested against the **Conduit family** (Conduit / conduwuit) — lightweight,
single-binary homeservers that are easy to self-host. The bridge only uses the
standard Matrix client-server API, so Synapse and Dendrite should work as well.
You need:

- a **bot** account (one per machine is recommended) with an access token, and
- a **human** account you chat from, which the bot invites to each room.

## Development

```bash
uv sync --python 3.12
uv run pytest tests/ -q
```

The repo is a `uv` workspace; each variant is a package under `packages/`.
`scripts/sync-to-global.sh` (wrapped by `just sync`) mirrors a local clone into
the Claude Code plugin cache for testing changes live.

## License

MIT © Nick Fujita ([github.com/nickfujita](https://github.com/nickfujita))
