# Use chat titles in tmux and Matrix

The plugin ships the `session-title` command and the `watch` loop that mirrors a
Claude Code or Codex session's native title into tmux window options and Matrix
room names. It does not install anything on the box. Box personalization —
the tmux formats, the `~/.local/bin/session-title` wrapper, the
`session-titles.service` user service, the Codex terminal-title setting, and
the agent instruction that names sessions — belongs to
[box-bootstrap](https://github.com/nickfujita/box-bootstrap):

```sh
./install.sh --session-titles          # wrapper, service, tmux formats, Codex setting
./install.sh --global-instructions     # the naming policy in ~/.codex/AGENTS.md
```

Both are also part of `./install.sh --agents`. The wrapper resolves the plugin
root from `~/.claude/plugins/installed_plugins.json` on every call, so a plugin
update moves the command and the service with it.

## What a box needs

Without box-bootstrap, provide the same five pieces by hand:

1. A `session-title` command on `PATH` that runs
   `uv run --no-sync --quiet --project <plugin root> session-title "$@"`.
2. A user service running `session-title watch`, restarted on failure.
3. tmux formats that name agent windows from `@session_title` and show
   `@session_title | @session_repo` on the right. box-bootstrap's copy is
   `dotfiles/tmux/session-titles.conf`.
4. `[tui] terminal_title = ["thread"]` in `~/.codex/config.toml`, so Codex
   publishes its thread name.
5. The naming policy in the agent's global instructions — box-bootstrap's copy
   is the `## Session titles` section of `dotfiles/codex/AGENTS.md.template`.

## Naming sessions

Use a short name for the overall task:

```sh
session-title get
session-title set "Fix VM hibernation"
```

The helper uses the CLI's exported session ID, or the Matrix bridge's unique
active session binding for the current pane. It never identifies sessions by
directory. If a nested CLI inherits both harnesses' IDs, specify `--agent` rather
than allowing the helper to guess. From outside the session, specify its identity:

```sh
session-title set "Fix VM hibernation" --agent codex --session SESSION_UUID
```

Keep titles stable through subtasks, debugging, testing, and branch changes.
Change the title when the main objective changes. Preserve a user-chosen title.
Only the main agent calls the helper. This naming policy is an agent instruction,
not an automatic classifier. Codex subagent targets and Claude sidechain targets
are rejected, but a Claude subagent inheriting its parent's environment must obey
the instruction not to rename its parent.

## What the watcher does

The tmux window list shows a shortened title. The right side shows the active
`task title | repository` with more space. The repository comes from the Git
remote, with the folder name as a fallback. A manual tmux window rename disables
automatic naming for that window, as usual. To follow the chat title again, run
this in that window:

```sh
tmux set-option -w automatic-rename on
```

The watcher checks native title metadata every three seconds and requires two
matching observations before publishing a change. Updates normally appear within
six seconds when Matrix is responsive. It only sends a Matrix name update when
the composed name differs from the last acknowledged value. Room names use
`repo · task title`; ended sessions retain their red marker. Sessions without a
native title retain the existing repository and branch fallback. No messages,
voice notifications, or additional title-generation model requests are sent by
the watcher.

Codex names come from its local session index. Renames use `thread/name/set`,
through the existing local app-server daemon when available, otherwise through
a short-lived standalone app-server. Neither path starts a model turn. Claude
names and renames use the official Python Agent SDK. The helper also queues a
pending Claude rename. A `UserPromptSubmit` or `SessionStart` hook returns it as
`sessionTitle`, making the live CLI adopt it instead of reappending a cached old
name. tmux and Matrix show the pending name immediately, subject to the watcher's
debounce. A different native rename supersedes the pending request. These
adapters currently support Codex's local session index and Claude's JSONL
session store. An existing private Codex TUI can retain its cached label until
it refreshes metadata. Claude's prompt adopts the name on the next submitted
prompt or resume. Restart existing Claude sessions once to load the new plugin
hooks. Existing sessions also need to reload their global instructions before
the naming policy reaches the model.

The service targets the default tmux server on this VM. Separate named tmux
servers need their own configuration and session mapping; the bridge's existing
pane IDs do not identify a tmux server. Do not share those maps between servers.

Inspect the service:

```sh
systemctl --user status session-titles.service
journalctl --user -u session-titles.service -n 30
```

To stop synchronization, run `systemctl --user disable --now session-titles.service`.
Native titles continue to work if the Matrix server is unavailable.

## Tests

Run the offline tests with the existing Antigravity package on the Python path:

```sh
PYTHONPATH=packages/antigravity-matrix/src uv run pytest tests
```

The title tests mock Matrix and use temporary native metadata files. Live
verification should use a separate tmux server and disabled bridge hooks so a
fixture cannot create a room or send a notification.

## Repository aliases

Matrix repository aliases are optional local settings in `~/.ccmatrix/config.json`.
Add a `repo_aliases` object alongside the existing settings, for example
`"repo_aliases": {"long-project-name": "short"}`. Keys match the repository name
from its origin remote, or its root folder when no origin exists. Unmapped
repositories keep their full name. Outside Git, rooms show only the task title,
or `Agent session` until a title is available. These aliases do not affect tmux.
Restart the Matrix daemons and `session-titles.service` after editing aliases.
Never commit your local configuration, which also contains credentials.
