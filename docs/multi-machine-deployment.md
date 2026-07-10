# Deploying to multiple machines

The bridge is designed to run on many machines at once — a laptop, a few dev
boxes, cloud instances — all mirroring their sessions into rooms on the same
Matrix homeserver. This doc covers how to give each machine its own identity and
roll the bridge out cleanly.

## One bot account per machine

Give each machine its **own** bot Matrix account with its own scoped access
token, for example `@mybot-a`, `@mybot-b`, `@mybot-cloud-1`. Per-machine
accounts mean:

- the room list shows which machine a room came from,
- a compromised or retired machine's token can be revoked without affecting the
  others,
- inbound routing (and the optional voicehub STT allowlist) can be scoped per
  machine.

The human you chat from is a single separate account (`admin_user_id`), invited
to every room across all machines.

## Machine identity (letter + color)

Each machine renders a per-machine avatar — a colored background with a letter
and a framework badge — plus `repo/branch` room names, so the room list tells
you which machine and repo a room belongs to.

### Letter

- **Default:** derived from the hostname — the last alphabetic character,
  uppercased (`workstation-a` → `A`).
- **Random/provider-assigned hostnames** (common on cloud hosts): set
  `CCMATRIX_VM_LETTER` explicitly in the environment before anything touches the
  bridge, e.g. in the shell profile:

  ```bash
  export CCMATRIX_VM_LETTER=S
  ```

### Colors

Letters map to a fixed color palette in
`packages/matrix-bridge-common/src/matrix_bridge/vm.py` (`_VM_COLORS`). Letters
not in the map fall back to a rotating palette. For predictable identity, add
your letter to `_VM_COLORS` before rollout:

| Letter | Color   | RGB              |
|--------|---------|------------------|
| `A`    | amber   | `(255, 153, 0)`  |
| `B`    | teal    | `(0, 194, 209)`  |
| `C`    | magenta | `(224, 57, 158)` |
| `D`    | purple  | `(140, 90, 230)` |
| `E`    | green   | `(90, 180, 90)`  |
| `F`    | blue    | `(33, 150, 243)` |
| `G`    | coral   | `(255, 111, 145)`|
| `H`    | violet  | `(156, 39, 176)` |
| `S`    | yellow  | `(255, 214, 10)` |

## Repo aliases

Long repo names can alias to friendlier room labels via
`MatrixConfig.repo_aliases` (empty by default). Add your own by editing
`~/.ccmatrix/config.json` on each machine:

```json
"repo_aliases": { "my-really-long-repo-name": "myrepo" }
```

## Proxy support

If a machine reaches the homeserver only through a local forward proxy, set
`proxy_url` in its config (e.g. `"http://127.0.0.1:1055"`); machines with a
direct connection leave it blank.

## Rollout runbook (per machine)

1. **Provision the bot account** on the homeserver and mint a scoped access
   token for it.
2. **Install/update the plugin** on the machine (via the marketplace, or by
   pulling this repo and running `just sync` if you develop against a local
   clone).
3. **Write `~/.ccmatrix/config.json`** with that machine's `homeserver`,
   `user_id`, `access_token`, `admin_user_id`, and (on cloud hosts) `proxy_url`.
   Set `CCMATRIX_VM_LETTER` in the environment if the hostname isn't
   distinctive. The file is re-saved at `0600` on first load.
4. **Restart the daemons** so inbound routing uses the current token/code:
   ```bash
   # Claude Code inbound daemon
   kill "$(cat ~/.ccmatrix/daemon.pid 2>/dev/null)" 2>/dev/null; rm -f ~/.ccmatrix/daemon.pid
   # Codex daemon
   kill "$(cat ~/.ccmatrix/codex-daemon.pid 2>/dev/null)" 2>/dev/null; rm -f ~/.ccmatrix/codex-daemon.pid
   # Antigravity daemon
   kill "$(cat ~/.ccmatrix/antigravity-daemon.pid 2>/dev/null)" 2>/dev/null; rm -f ~/.ccmatrix/antigravity-daemon.pid
   ```
   The daemons re-spawn on the next hook invocation. To start them by hand, run
   the enable flow, e.g.
   `uv run --project ${CLAUDE_PLUGIN_ROOT} codex-matrix enable`.
5. **Re-apply avatars / room names:**
   ```bash
   uv run --project packages/matrix-bridge-common python -m matrix_bridge.tools.refresh_rooms
   ```
6. **(Optional) voice:** if you run the `matrix-voicehub` appservice, add this
   machine's bot account to its STT allowlist so inbound voice notes reach the
   terminal.

## Verify

- Room list shows `repo/branch` names with the correct per-machine letter/color.
- A **text** message from your Matrix client reaches the terminal within ~1s.
- `stat -c '%a' ~/.ccmatrix/config.json` → `600`.
- With voicehub deployed: a completed turn produces spoken audio in the room,
  and a voice note is transcribed and reaches the terminal.

## Manual one-shot room refresh

```bash
uv run --project packages/matrix-bridge-common python -m matrix_bridge.tools.refresh_rooms
```

(See `packages/matrix-bridge-common/src/matrix_bridge/tools/refresh_rooms.py`.)
