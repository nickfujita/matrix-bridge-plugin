# matrix-bridge-plugin

A self-hosted **Matrix bridge for AI coding CLIs**. It mirrors each coding
session into a room on *your own* Matrix homeserver, so you can watch progress
and chat back — from your phone, a tablet, or any Matrix client — while the
agent keeps working on your machine.

One plugin, `matrix-bridge-plugin`, installed on each harness from the same
marketplace entry, with three framework variants as packages in this repo:

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

The plugin is distributed through this repo's plugin marketplace. Both
harnesses read the same manifest, so install it on each one you use. Claude
Code:

```
/plugin marketplace add nickfujita/matrix-bridge-plugin
/plugin install matrix-bridge-plugin
```

Codex:

```bash
codex plugin marketplace add nickfujita/matrix-bridge-plugin
codex plugin add matrix-bridge-plugin@matrix-bridge-plugin
```

Each harness keeps its own copy under its plugin cache and runs its own
daemon from it. Then configure it:

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

## Upgrading from `claude-code-matrix`

Releases before 0.8.0 registered the plugin and its marketplace as
`claude-code-matrix`, the name of the Claude Code variant package. Both are now
`matrix-bridge-plugin`, the name of this repo, and a plugin manager treats that
as a different plugin: remove the old one and install the new one on each
harness.

```
# Claude Code
/plugin uninstall claude-code-matrix@claude-code-matrix
/plugin marketplace remove claude-code-matrix
/plugin marketplace add nickfujita/matrix-bridge-plugin
/plugin install matrix-bridge-plugin
```

```bash
# Codex
codex plugin remove claude-code-matrix@claude-code-matrix
codex plugin marketplace remove claude-code-matrix
codex plugin marketplace add nickfujita/matrix-bridge-plugin
codex plugin add matrix-bridge-plugin@matrix-bridge-plugin
```

Then run `codex-matrix enable` once more so the Codex notify script resolves the
plugin under its new cache directory, and restart the bridge daemons and any
`session-title watch` service. `~/.ccmatrix/` state, room mappings, and your
configuration are untouched by the rename. Skills move with the plugin name:
`/claude-code-matrix:go-mobile` becomes `/matrix-bridge-plugin:go-mobile`.

## Skills

The plugin ships two skills under `skills/`. Both are bridge concerns rather
than development workflow, so they travel with the bridge.

- **`go-mobile`** switches every later reply to a spoken, TTS-safe style, so a
  turn read aloud on a phone stays listenable. An optional `repeat` argument
  re-delivers the previous reply in spoken form.
- **`stop-mobile`** ends that mode and restores normal formatting.

They live here because the session sentinel below names `go-mobile` in the
context it injects. Keeping the skill and the hook under one version stops the
hook from widening a trigger the skill deliberately narrowed.

Both harnesses read `skills/` straight from the installed plugin root, so there
is no extra install step and nothing to copy into `~/.claude/skills` or
`~/.codex/skills`. Claude Code picks them up from its plugin cache after
`/plugin install`, and Codex picks them up from `~/.codex/plugins/cache/` once
the plugin is installed from the same marketplace entry.

## Session sentinel

Two rules only the bridge knows about, so the bridge teaches them. While a
bridge is enabled, `hooks/session-sentinel.sh` injects roughly two lines of
`SessionStart` context (~90 tokens, once per session) telling the agent to:

- **switch to a spoken, TTS-safe reply style for replies that go back out over
  the bridge** by invoking the bundled `go-mobile` skill. A message that
  arrived through the bridge is a real mobile signal. Dictation artifacts and
  typos are not, because the operator dictates at the desktop too. Any other
  time, the operator asks for `/go-mobile` explicitly, and
- **never start a nested `claude`/`codex` session in the bridge-owned tmux
  pane.** Only one live session may own a pane, so a nested one silently
  retires the real session's mapping: outbound messages keep flowing while
  inbound phone → terminal replies stop arriving. Use a separate tmux session
  or a container instead.

The hook is plain `bash` — no `uv`, no Python — and prints nothing at all when
neither `~/.ccmatrix/enabled` nor `~/.ccmatrix/codex-enabled` is present, so a
disabled install costs zero context. It lives in `hooks/hooks.json`, which both
Claude Code and Codex read automatically.

The payload is deliberately terse. Claude Code absorbs `additionalContext`
silently, but Codex renders the same string back to the user as a
`hook context:` block on the first turn of every session, and it ignores
`suppressOutput` on `SessionStart`, so there is no quiet channel — length is the
only lever. The rules stay identical on both harnesses; only the prose is cut.

## Which sessions get a room

`hooks/hooks.json` being read by both harnesses is what the sentinel wants, but
it applies to every hook in the file. Install this plugin under Codex as well
and Codex will happily run the *Claude* handlers too — it records them in
`~/.codex/config.toml` as
`hooks.state."matrix-bridge-plugin@matrix-bridge-plugin:hooks/hooks.json:<event>:i:j"`
— handing them a payload whose `session_id` is a Codex thread id. Left
unchecked, every Codex session got a second, Claude-avatar room on top of the
one the Codex bridge already made: two rooms, two notifications and two spoken
replies for one unit of work.

So each Claude handler first asks whether the session is its own. The test is
positive rather than a guess about the caller: Claude Code puts a
`transcript_path` under its projects directory in every hook payload, and
Codex's points at a rollout file under `CODEX_HOME`. A payload that fails the
test is ignored, and any entry an older version left in `sessions.json` for that
id is retired so the daemon stops routing phone replies into it.

The second question is whether the session is addressed to a human at all. A
session that another agent's flow spawned — the interactive `claude` a Codex
skill starts in tmux for a review, a scripted persona run — is a work product
for that agent, and mirroring it just puts a room and a spoken reply in front of
someone who never asked for it. Such a spawner exports
`CCMATRIX_SUPPRESS_SESSION=1` before launching the CLI; every child process
inherits it, and the bridge then creates no room, sends no notification, emits
no `cc.tts` tag and injects no sentinel context for that session.

```bash
# in a script that launches an agent CLI for its own consumption
CCMATRIX_SUPPRESS_SESSION=1 tmux new-session -d -s reviewer claude
```

Codex answers the same question from the rollout instead of the environment,
because it has to: its bridge discovers most sessions with a filesystem watcher
over `~/.codex/sessions/`, running in a long-lived daemon that never sees the
environment of whatever spawned the CLI. Two kinds of session are kept off the
phone, both read out of the `session_meta` line Codex writes itself:

- **background subagent threads**, marked `thread_source=subagent` with a parent
  thread id — internal work for the parent agent;
- **`codex exec` runs**, marked `source=exec` / `originator=codex_exec` — a
  script started them, they run one turn and exit. Nobody is at a terminal, and
  inbound phone replies are typed into a tmux pane, so an exec room could never
  be answered even if you tried. One automation round used to open several.

To put a scripted run back on the phone, put `CCMATRIX_FORCE_MIRROR` in its
prompt:

```bash
codex exec "CCMATRIX_FORCE_MIRROR
Summarise this morning's alerts"
```

It is a prompt marker rather than an environment variable for the reason above —
the rollout is the only channel that reaches the watcher. Codex does write an
`<environment_context>` block into the transcript, but it carries cwd, shell,
date, timezone and filesystem roots only, so no exported variable can be
recovered from the file. The trade-off is that the marker is part of the prompt
the model reads; keep it on its own line. See
`codex_matrix.transcript.is_unmirrored_session`.

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
| `CCMATRIX_SUPPRESS_SESSION` | (Claude Code / Antigravity) Set to `1` by whatever *spawns* an agent CLI programmatically. The spawned session gets no room, no notification, no TTS and no sentinel context — see [Which sessions get a room](#which-sessions-get-a-room). Unset/`0`/`false`/`no`/`off` all mean "normal human session". |

Codex has no environment-variable equivalent — see the section above. Its
`codex exec` runs are suppressed from rollout metadata, and `CCMATRIX_FORCE_MIRROR`
in the prompt is the opt-in.

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

MIT © [nickfujita](https://github.com/nickfujita)
