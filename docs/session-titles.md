# Use chat titles in tmux and Matrix

Install from a durable checkout on each Linux VM with tmux, uv, and a running
systemd user manager. Configure the existing Matrix bridge first if you want
room names synchronized.

```sh
scripts/sync-to-global.sh --no-refresh-rooms
uv run scripts/install-session-titles.py --adopt-windows
```

The first command installs the updated bridge naming functions. The second
installs `~/.local/bin/session-title`, the `session-titles.service` user service,
tmux formats, the Codex terminal-title setting, and a managed section in
`~/.codex/AGENTS.md`. Claude Code must import that file in its global `CLAUDE.md`,
as the operator's shared setup already does. The installer backs up changed
files. Keep the checkout in place because the command wrapper runs its code.

`--adopt-windows` enables automatic names for existing windows whose foreground
command is `codex` or `claude`. It records their previous names in
`~/.ccmatrix/tmux-windows-before-titles-*.tsv`. Omit the flag on subsequent installs
to preserve manual window-name overrides.

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

The tmux window list shows a shortened title. The right side shows the active
`repository | task title` with more space. The repository comes from the Git
remote, with the folder name as a fallback. A manual tmux window rename disables automatic naming for
that window, as usual. To follow the chat title again, run this in that window:

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
debounce. A different native rename supersedes the pending request. These adapters currently
support Codex's local session index and Claude's JSONL session store. An existing
private Codex TUI can retain its cached label until it refreshes metadata. Claude's
prompt adopts the name on the next submitted prompt or resume. Restart existing
Claude sessions once to load the new plugin hooks. Existing sessions also need
to reload their global instructions before the naming policy reaches the model.

The user service targets the default tmux server on this VM. Separate named tmux
servers need their own configuration and session mapping; the bridge's existing
pane IDs do not identify a tmux server. Do not share those maps between servers.

Inspect the service:

```sh
systemctl --user status session-titles.service
journalctl --user -u session-titles.service -n 30
```

To stop synchronization, run `systemctl --user disable --now session-titles.service`.
Restore the backed-up configuration files to undo the display and instruction
changes. The installer does not alter Claude's terminal-title settings. Native
titles continue to work if the Matrix server is unavailable.

Run the offline tests with the existing Antigravity package on the Python path:

```sh
PYTHONPATH=packages/antigravity-matrix/src uv run pytest tests
```

The title tests mock Matrix and use temporary native metadata files. Live
verification should use a separate tmux server and disabled bridge hooks so a
fixture cannot create a room or send a notification.
