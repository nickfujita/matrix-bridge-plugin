# Stable runtime installation

Plugin caches are versioned directories. A running daemon can keep imports from
a removed directory even when its process looks healthy. On a machine with a
supervised source installation, keep the runtime checkout separate from the
plugin manager's cache.

## Pin the completion handler

Write the absolute checkout path to `~/.ccmatrix/runtime-root`, then run
`codex-matrix enable` from the tested checkout. Generated completion hooks honor
this file before choosing a cached release. `CCMATRIX_RUNTIME_ROOT` overrides the
file for an explicit process invocation. A missing or invalid pinned checkout
fails instead of silently running an older cache release.

The pin is local configuration. Do not commit credentials or machine-specific
paths. Normal installations without a pin still follow their installed
marketplace revision and semantic version selection.

## Supervise the daemon

Configure the user service's working directory and Python module invocation to
use the same checkout. Build its environment once with
`uv sync --all-packages --project <checkout>`, and use `uv run --no-sync` in
long-lived services. This avoids concurrent dependency synchronization while
services run. Keep generic machine launchers under `~/.agents`, outside plugin
caches.

Use `python -m codex_matrix` for the daemon and
`python -m matrix_bridge.session_title watch` for title synchronization. Do not
invoke a `session-title` wrapper by the same console command inside `uv run`.
If the environment lacks that entrypoint, it can find the outer wrapper again
and recurse.

## Verify an update

1. Run the routing, notification, lifecycle and runtime-resolution tests.
2. Confirm the pinned checkout, notify handler and service agree on the release.
3. Restart only the services whose code changed. Preserve the enabled flag,
   current session-to-room map and recent sync cursor.
4. Check service restart counters and the active session's pane and room.
5. Send a phone message and confirm one routing event and one pane injection.
   Check the resulting reply in the phone client too.

No installation can guarantee future faults never happen. These checks catch
the observed child/parent routing regression and cache replacement faults.
