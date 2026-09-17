"""CLI entrypoint for Codex Matrix bridge management."""

import argparse
import os
import re
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout
from matrix_bridge.config import load_config, STATE_DIR

from .daemon_lifecycle import (
    PID_FILE_NAME,
    running_daemon_pid,
    start_daemon,
    stop_daemon,
)


CODEX_STATE_DIR = STATE_DIR  # ~/.ccmatrix
ENABLED_FLAG = CODEX_STATE_DIR / "codex-enabled"
OPERATION_LOCK_NAME = "codex-cli-operation.lock"
OPERATION_LOCK_TIMEOUT_SECONDS = 5

# The generated wrapper records the passthrough notifier here so a later
# `codex-matrix enable` can recover it without parsing shell.
_PASSTHROUGH_MARKER = "# matrix-bridge:passthrough "
_PASSTHROUGH_TIMEOUT_SECONDS = 2
_PASSTHROUGH_KILL_GRACE_SECONDS = 1


def _acquire_operation_lock() -> FileLock | None:
    """Serialize an explicit command's complete state transition."""
    CODEX_STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        str(CODEX_STATE_DIR / OPERATION_LOCK_NAME), timeout=OPERATION_LOCK_TIMEOUT_SECONDS
    )
    try:
        lock.acquire()
    except Timeout:
        print("Timed out waiting for Codex lifecycle operation lock.")
        return None
    return lock


def _start_daemon_raw():
    return start_daemon(CODEX_STATE_DIR)


def _stop_daemon_raw():
    return stop_daemon(CODEX_STATE_DIR)


def _report_start_result(result) -> int:
    if result.state == "already_running":
        print(f"Codex daemon already running (PID {result.pid})")
        return 0
    if result.state == "started":
        print(f"Codex daemon started (PID {result.pid})")
        return 0
    detail = f": {result.detail}" if result.detail else ""
    print(f"Codex daemon failed to become ready{detail}")
    return 1


def _report_stop_result(result) -> int:
    if result.state == "not_running":
        print("Codex daemon not running (stale or malformed PID file).")
        return 0
    if result.state == "stopped":
        print(f"Codex daemon stopped (PID {result.pid})")
        return 0
    detail = f": {result.detail}" if result.detail else ""
    print(f"Failed to stop Codex daemon{detail}")
    return 1


def cmd_enable(args):
    """Enable the Codex Matrix bridge."""
    config = load_config()
    if not config:
        print("Not configured. Run 'ccmatrix setup' first.")
        return 1

    operation_lock = _acquire_operation_lock()
    if operation_lock is None:
        return 1
    try:
        return _cmd_enable_locked(args)
    finally:
        operation_lock.release()


def _cmd_enable_locked(args) -> int:
    was_enabled = ENABLED_FLAG.exists()

    # First installs must not publish enabled state until their hook and daemon
    # are both usable. A re-enable deliberately retains its previous flag
    # throughout the repair attempt so a failed repair cannot turn it off.
    if not _install_notify_hook():
        print("Codex Matrix bridge was not enabled because its notify hook could not be installed.")
        return 1

    # Start daemon
    result = _start_daemon_raw()
    if _report_start_result(result):
        print("Codex Matrix bridge was not enabled because its daemon failed to start.")
        return 1

    if not was_enabled:
        try:
            ENABLED_FLAG.touch()
        except OSError as error:
            try:
                ENABLED_FLAG.unlink(missing_ok=True)
            except OSError:
                pass

            detail = f"failed to publish enabled state: {error}"
            if result.state == "started":
                rollback = _stop_daemon_raw()
                if rollback.state not in {"stopped", "not_running"}:
                    rollback_detail = rollback.detail or rollback.state
                    detail += f"; daemon rollback could not prove exit: {rollback_detail}"
                else:
                    detail += "; stopped daemon started by this enable"
            print(f"Codex Matrix bridge was not enabled because {detail}.")
            return 1
    print("Codex Matrix bridge enabled.")
    return 0


def cmd_disable(args):
    """Disable the Codex Matrix bridge."""
    operation_lock = _acquire_operation_lock()
    if operation_lock is None:
        return 1
    try:
        return _cmd_disable_locked(args)
    finally:
        operation_lock.release()


def _cmd_disable_locked(args) -> int:
    was_enabled = ENABLED_FLAG.exists()
    # Removing this before stop closes the notify/start race. If stopping does
    # not prove the daemon is gone, restore exactly the prior enabled state.
    ENABLED_FLAG.unlink(missing_ok=True)
    result = _stop_daemon_raw()
    if _report_stop_result(result):
        if was_enabled:
            try:
                ENABLED_FLAG.touch()
            except OSError as restore_error:
                second_stop = _stop_daemon_raw()
                if second_stop.state in {"stopped", "not_running"}:
                    print(
                        "Codex Matrix bridge disabled after enabled-flag restoration failed "
                        f"({restore_error}); second safe stop proved daemon exit."
                    )
                    return 0
                second_detail = second_stop.detail or second_stop.state
                print(
                    "Codex Matrix bridge remains with daemon state unproven: "
                    f"enabled-flag restore failure: {restore_error}; "
                    f"second stop failure: {second_detail}."
                )
                return 1
        print("Codex Matrix bridge remains enabled because its daemon could not be stopped.")
        return 1
    print("Codex Matrix bridge disabled.")
    return 0


def cmd_start(args):
    """Start the Codex daemon."""
    operation_lock = _acquire_operation_lock()
    if operation_lock is None:
        return 1
    try:
        return _report_start_result(_start_daemon_raw())
    finally:
        operation_lock.release()


def cmd_stop(args):
    """Stop the Codex daemon."""
    operation_lock = _acquire_operation_lock()
    if operation_lock is None:
        return 1
    try:
        return _report_stop_result(_stop_daemon_raw())
    finally:
        operation_lock.release()


def cmd_status(args):
    """Show Codex bridge status."""
    config = load_config()
    if not config:
        print("Not configured. Run 'ccmatrix setup' first.")
        return 1

    enabled = ENABLED_FLAG.exists()
    print(f"Codex bridge: {'enabled' if enabled else 'disabled'}")

    pid_file = CODEX_STATE_DIR / PID_FILE_NAME
    pid = running_daemon_pid(CODEX_STATE_DIR)
    if pid is not None:
        print(f"Daemon: running (PID {pid})")
    elif pid_file.exists():
        print("Daemon: not running (stale or malformed PID file)")
    else:
        print("Daemon: not running")

    # Show active Codex sessions
    from matrix_bridge.session import SessionMap
    session_map = SessionMap(CODEX_STATE_DIR / "codex-sessions.json")
    active = session_map.active_sessions()
    print(f"\nActive Codex sessions: {len(active)}")
    for entry in active:
        project = Path(entry.cwd).name if entry.cwd else "?"
        room = entry.room_id or "no room"
        print(f"  {entry.session_id[:8]}... | pane {entry.tmux_pane} | {project} | {room}")

    # Check notify hook
    _check_notify_hook()
    return 0


def _install_notify_hook() -> bool:
    """Install the Codex notify fan-out wrapper in Codex config.toml.

    A boolean outcome lets ``enable`` keep its persistent state aligned with
    the actual Codex config rather than treating a warning as success.
    """
    import tomllib

    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        print("Warning: ~/.codex/config.toml not found. Codex may not be installed.")
        return False

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)

        notify = config.get("notify", [])
        wrapper_path = _get_notify_wrapper_path()
        passthrough_command = _existing_notify_command(notify)
        configured = _notify_points_to_wrapper(notify)
        if configured:
            passthrough_command = _existing_wrapper_passthrough_command() or passthrough_command

        content = config_path.read_text()
        notify_line = f'notify = [ "{wrapper_path}" ]'
        if configured:
            replacement = content
        elif re.search(r"(?m)^notify\s*=\s*\[[^\n]*\]\s*$", content):
            replacement = re.sub(
                r"(?m)^notify\s*=\s*\[[^\n]*\]\s*$", notify_line, content, count=1
            )
        elif "notify" not in content:
            replacement = _insert_top_level_key(content, notify_line)
        else:
            print("Warning: 'notify' exists in config.toml but is not a single-line array.")
            print(f"Please manually set it to: {notify_line}")
            return False

        updates = _notify_script_updates(passthrough_command)
        if not configured:
            updates.append((config_path, replacement.encode("utf-8"), _file_mode(config_path)))
        if not _commit_file_updates(updates):
            print("Warning: failed to install Codex notify hook without changing live files.")
            return False
        if configured:
            print("Notify hook already configured.")
        else:
            print("Added notify hook to ~/.codex/config.toml")
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"Warning: failed to install Codex notify hook: {error}")
        return False
    return True


def _insert_top_level_key(content: str, key_line: str) -> str:
    """Insert a bare `key = value` line so it stays TOP-LEVEL in TOML.

    A bare key appended to the end of a TOML file does NOT become a top-level
    key — it belongs to whichever table header was declared last. A Codex
    config ending in `[agents]` would therefore parse `notify` as an agent role
    and Codex refuses to start:

        invalid length 1, expected struct AgentRoleToml with 3 elements in `agents`

    So the line must go before the first table header, not at the end.
    """
    lines = content.splitlines(keepends=True)
    first_table = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("[")),
        len(lines),
    )
    if first_table == len(lines):
        # No tables at all — appending is safe, but keep the file newline-clean.
        prefix = "" if content.endswith("\n") or not content else "\n"
        return f"{content}{prefix}{key_line}\n"
    lines.insert(first_table, f"{key_line}\n\n")
    return "".join(lines)


def _existing_notify_command(notify: object) -> str | None:
    """Return the existing non-Matrix notify command, preserving argv semantics."""
    if not isinstance(notify, list):
        return None

    matrix_paths = set(_get_notify_script_candidates())
    matrix_script_names = {"codex-notify.sh", "codex-notify-wrapper.sh"}
    filtered = [
        str(item)
        for item in notify
        if str(item) not in matrix_paths and Path(str(item)).name not in matrix_script_names
    ]
    if not filtered:
        return None
    return shlex.join(filtered)


def _notify_points_to_wrapper(notify: object) -> bool:
    if not isinstance(notify, list):
        return False
    wrapper_path = _get_notify_wrapper_path()
    return len(notify) == 1 and str(notify[0]) == wrapper_path


def _existing_wrapper_passthrough_command() -> str | None:
    wrapper = Path(_get_notify_wrapper_path())
    matrix_notify = _get_notify_script_path()
    if not wrapper.exists():
        return None

    text = wrapper.read_text()

    # Wrappers written by this version record the passthrough verbatim, because
    # the invocation line now goes through a shell variable and is no longer
    # readable as a command.
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_PASSTHROUGH_MARKER):
            return stripped[len(_PASSTHROUGH_MARKER):].strip() or None

    # Older wrappers invoked the passthrough directly; recover it from the line.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or matrix_notify in stripped:
            continue
        marker = ' "$@"'
        if marker in stripped:
            return stripped.split(marker, 1)[0]
    return None


def _passthrough_candidate_exprs(argv0: str) -> list[str]:
    """Shell-quoted paths to try for the passthrough notifier, in order.

    The wrapper records an absolute path, but installers relocate these
    scripts — box-bootstrap now drops them in ~/.local/bin where they used to
    land in /usr/local/bin. Baking in one path turns a relocated notifier into
    a wrapper that can never fire again, so emit the alternatives too and let
    the wrapper pick at run time.
    """
    name = Path(argv0).name
    exprs: list[str] = []
    seen: set[str] = set()

    def add(expr: str, key: str) -> None:
        if key not in seen:
            seen.add(key)
            exprs.append(expr)

    add(shlex.quote(argv0), argv0)
    add(shlex.quote(f"/usr/local/bin/{name}"), f"/usr/local/bin/{name}")
    # $HOME has to expand when the wrapper runs, not when it is written, so it
    # stays in double quotes while the basename is quoted normally.
    add(f'"$HOME/.local/bin"/{shlex.quote(name)}', f"$HOME/.local/bin/{name}")
    return exprs


@dataclass(frozen=True)
class _FileSnapshot:
    content: bytes | None
    mode: int | None


def _file_mode(path: Path, default: int = 0o600) -> int:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return default


def _snapshot_file(path: Path) -> _FileSnapshot:
    try:
        return _FileSnapshot(path.read_bytes(), _file_mode(path))
    except OSError:
        return _FileSnapshot(None, None)


def _stage_file(path: Path, content: bytes, mode: int) -> Path:
    """Write a complete replacement beside its target, never into it."""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary, mode)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _restore_snapshot(path: Path, snapshot: _FileSnapshot) -> None:
    """Restore one committed target without clobbering a separately staged file."""
    if snapshot.content is None or snapshot.mode is None:
        path.unlink(missing_ok=True)
        return
    temporary = _stage_file(path, snapshot.content, snapshot.mode)
    try:
        os.replace(temporary, path)
        os.chmod(path, snapshot.mode)
    finally:
        temporary.unlink(missing_ok=True)


def _commit_file_updates(updates: list[tuple[Path, bytes, int]]) -> bool:
    """Stage every update, then replace-or-restore the complete hook set."""
    snapshots = {path: _snapshot_file(path) for path, _content, _mode in updates}
    staged: dict[Path, Path] = {}
    committed: list[Path] = []
    try:
        for path, content, mode in updates:
            staged[path] = _stage_file(path, content, mode)
        for path, _content, mode in updates:
            temporary = staged.pop(path)
            os.replace(temporary, path)
            committed.append(path)
            os.chmod(path, mode)
    except OSError:
        restored = True
        for path in reversed(committed):
            try:
                _restore_snapshot(path, snapshots[path])
            except OSError:
                restored = False
        if not restored:
            # The caller still receives failure. Retaining temp files is safer
            # than overwriting a path after a repeated filesystem failure.
            return False
        return False
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return True


def _notify_script_updates(passthrough_command: str | None) -> list[tuple[Path, bytes, int]]:
    """Build, but do not yet replace, the Matrix handler and fan-out wrapper."""
    state_dir = Path.home() / ".ccmatrix"
    state_dir.mkdir(parents=True, exist_ok=True)

    project_root = Path(__file__).resolve().parents[4]
    matrix_notify = Path(_get_notify_script_path())
    wrapper = Path(_get_notify_wrapper_path())

    matrix_content = (
        "#!/bin/bash\n"
        "# Codex Matrix bridge notify handler - called by Codex on agent-turn-complete.\n"
        'echo "$(date) notify called with: ${1:0:200}" >> '
        f"{shlex.quote(str(state_dir / 'codex-notify.log'))}\n"
        + "\n".join(_plugin_root_resolution_lines(project_root)) + "\n"
        'cd "$matrix_root" || exit 1\n'
        'uv run --quiet python -c "from codex_matrix.notify_handler import handle_notify; handle_notify()" "$@" '
        f"2>> {shlex.quote(str(state_dir / 'codex-notify.log'))}\n"
    )

    wrapper_lines = [
        "#!/bin/bash",
        "# Codex supports one notify command. Fan out to the pre-existing",
        "# completion notifier and the Matrix bridge notify handler.",
    ]
    if passthrough_command:
        argv = shlex.split(passthrough_command)
        candidates = " ".join(_passthrough_candidate_exprs(argv[0]))
        extra_args = f"{shlex.join(argv[1:])} " if len(argv) > 1 else ""
        wrapper_lines += [
            "",
            f"{_PASSTHROUGH_MARKER}{passthrough_command}",
            "# Resolve the passthrough notifier at run time: installers move it",
            "# between /usr/local/bin and ~/.local/bin. Skip it silently when no",
            "# copy is present, so a missing notifier never fails the notify hook.",
            "matrix_passthrough=''",
            f"for candidate in {candidates}; do",
            '  if [[ -x "$candidate" ]]; then',
            '    matrix_passthrough="$candidate"',
            "    break",
            "  fi",
            "done",
            'if [[ -n "$matrix_passthrough" ]]; then',
            "  if command -v timeout >/dev/null 2>&1; then",
            f'    timeout --kill-after={_PASSTHROUGH_KILL_GRACE_SECONDS}s {_PASSTHROUGH_TIMEOUT_SECONDS}s '
            f'"$matrix_passthrough" {extra_args}"$@" '
            f">> {shlex.quote(str(state_dir / 'codex-notify-wrapper.log'))} 2>&1 || true",
            "  else",
            f'    "$matrix_passthrough" {extra_args}"$@" '
            f">> {shlex.quote(str(state_dir / 'codex-notify-wrapper.log'))} 2>&1 || true",
            "  fi",
            "fi",
            "",
        ]
    wrapper_lines.append(
        f"{shlex.quote(str(matrix_notify))} \"$@\" >> {shlex.quote(str(state_dir / 'codex-notify-wrapper.log'))} 2>&1 || true"
    )
    wrapper_content = "\n".join(wrapper_lines) + "\n"
    return [
        (matrix_notify, matrix_content.encode("utf-8"), 0o755),
        (wrapper, wrapper_content.encode("utf-8"), 0o755),
    ]


def _write_notify_scripts(passthrough_command: str | None) -> None:
    """Compatibility helper for callers that only need transactional scripts."""
    if not _commit_file_updates(_notify_script_updates(passthrough_command)):
        raise OSError("failed to update Codex notify scripts transactionally")


_VERSION_DIR_RE = re.compile(r"^v?\d+(\.\d+)+$")

# Relative path that proves a candidate directory really is a plugin checkout.
_PLUGIN_MARKER = "packages/codex-matrix/src/codex_matrix/notify_handler.py"
_PLUGIN_MANIFEST = ".claude-plugin/plugin.json"
_PLUGIN_NAME = "matrix-bridge-plugin"
_PLUGIN_INSTALL_METADATA = ".codex-marketplace-install.json"


# This is embedded in the generated notify script because the script needs to
# resolve a later installed plugin release without importing the old release it
# was written from. Keep its inputs as argv rather than shell-expanded paths:
# cache roots and version directories can contain whitespace.
_PLUGIN_ROOT_RESOLVER = r'''
import json
import re
import sys
import tomllib
from pathlib import Path

NAME = "matrix-bridge-plugin"
MARKER = Path("packages/codex-matrix/src/codex_matrix/notify_handler.py")
MANIFEST = Path(".claude-plugin/plugin.json")
INSTALL_METADATA = Path(".codex-marketplace-install.json")
VERSION = re.compile(r"^v?\d+(?:\.\d+)+$")


def normal_version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        return None
    return value[1:] if value.startswith("v") else value


def version_key(value):
    return tuple(int(part) for part in value.split("."))


def has_no_symlink_components(path):
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                return False
        except OSError:
            return False
    return True


def regular_contained_file(candidate, relative):
    target = candidate / relative
    if not target.is_file() or not has_no_symlink_components(target):
        return None
    try:
        real_candidate = candidate.resolve(strict=True)
        real_target = target.resolve(strict=True)
    except OSError:
        return None
    return target if real_target.is_relative_to(real_candidate) else None


def candidate_details(candidate, versions_dir):
    version = normal_version(candidate.name)
    if version is None or candidate.parent != versions_dir:
        return None
    if not candidate.is_dir() or not has_no_symlink_components(candidate):
        return None
    marker = regular_contained_file(candidate, MARKER)
    manifest_path = regular_contained_file(candidate, MANIFEST)
    if marker is None or manifest_path is None:
        return None
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("name") != NAME
        or normal_version(manifest.get("version")) != version
    ):
        return None
    revision = None
    metadata_path = regular_contained_file(candidate, INSTALL_METADATA)
    if metadata_path is not None:
        try:
            metadata = json.loads(metadata_path.read_text())
            if isinstance(metadata, dict) and isinstance(metadata.get("revision"), str):
                revision = metadata["revision"]
        except (OSError, ValueError):
            revision = None
    return version_key(version), str(candidate), revision


def active_revision(config_path):
    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
        marketplaces = config.get("marketplaces", {})
        marketplace = marketplaces.get(NAME, {}) if isinstance(marketplaces, dict) else {}
        revision = marketplace.get("last_revision") if isinstance(marketplace, dict) else None
        return revision if isinstance(revision, str) else None
    except (OSError, tomllib.TOMLDecodeError):
        return None


fallback = Path(sys.argv[1])
config_path = Path(sys.argv[2])
candidates = []
seen = set()
for priority, raw_versions_dir in enumerate(sys.argv[3:]):
    versions_dir = Path(raw_versions_dir)
    if not versions_dir.is_dir() or not has_no_symlink_components(versions_dir):
        continue
    try:
        children = list(versions_dir.iterdir())
    except OSError:
        continue
    for candidate in children:
        details = candidate_details(candidate, versions_dir)
        if details is not None and details[1] not in seen:
            seen.add(details[1])
            candidates.append((details[0], priority, details[1], details[2]))

revision = active_revision(config_path)
if revision is not None:
    active_candidates = [candidate for candidate in candidates if candidate[3] == revision]
    if active_candidates:
        candidates = active_candidates
    else:
        candidates = []

if candidates:
    print(max(candidates, key=lambda candidate: (candidate[0], candidate[1], candidate[2]))[2])
else:
    print(fallback)
'''


def _codex_plugin_cache() -> Path:
    """Return Codex's versioned cache location for this plugin."""
    return (
        Path.home()
        / ".codex"
        / "plugins"
        / "cache"
        / "matrix-bridge-plugin"
        / "matrix-bridge-plugin"
    )


def _plugin_root_resolution_lines(project_root: Path) -> list[str]:
    """Emit shell that sets `matrix_root` to the plugin root to run from.

    This script is written once, when the bridge is enabled, but it is executed
    on every turn for as long as the plugin is installed. A plugin manager
    installs each release into its own versioned directory, so baking in the
    directory that happened to be current at enable time means the hook keeps
    calling into that version forever while the daemon moves on — observed in
    the field as a hook pinned to 0.5.7 against a 0.5.10 daemon, with nothing
    that would ever refresh it.

    The resolver chooses the active marketplace revision when Codex records
    one, otherwise the highest numeric semantic version. A candidate only wins
    if its marker and manifest are regular, contained files; the path recorded
    at enable time remains the fallback for source-only development installs.
    """
    # A developer may run `enable` from a plain checkout, but Codex invokes the
    # generated script long after that command returns. Prefer a valid release
    # from its installed cache at execution time; retaining project_root as the
    # fallback keeps source-only development installs working.
    codex_plugin_cache = _codex_plugin_cache()
    versions_dirs = [codex_plugin_cache]
    if _VERSION_DIR_RE.match(project_root.name) and project_root.parent != codex_plugin_cache:
        versions_dirs.append(project_root.parent)

    resolver_args = [
        str(project_root),
        str(Path.home() / ".codex" / "config.toml"),
        *(str(versions_dir) for versions_dir in versions_dirs),
    ]
    resolver_command = "matrix_root=$(python3 -c {} {})".format(
        shlex.quote(_PLUGIN_ROOT_RESOLVER),
        " ".join(shlex.quote(argument) for argument in resolver_args),
    )
    return [
        "# Resolve the plugin root at run time. Installing a new version creates",
        "# a new directory beside this one, and this script is not rewritten on",
        "# upgrade — a hard-coded version would keep calling the version that was",
        "# current when the bridge was enabled.",
        resolver_command,
        '[[ -n "$matrix_root" ]] || matrix_root=' + shlex.quote(str(project_root)),
    ]


def _get_notify_script_path() -> str:
    """Get the path to the notify handler script."""
    return str(Path.home() / ".ccmatrix" / "codex-notify.sh")


def _get_notify_wrapper_path() -> str:
    """Get the path to the Codex notify fan-out wrapper."""
    return str(Path.home() / ".ccmatrix" / "codex-notify-wrapper.sh")


def _get_notify_script_candidates() -> list[str]:
    """Paths that should count as a valid Codex Matrix notify hook."""
    state_dir = Path.home() / ".ccmatrix"
    return [
        str(state_dir / "codex-notify.sh"),
        str(state_dir / "codex-notify-wrapper.sh"),
    ]


def _check_notify_hook():
    """Check if the notify hook is configured in Codex."""
    import tomllib

    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        print("\nNotify hook: ~/.codex/config.toml not found")
        return

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    notify = config.get("notify", [])
    handler_paths = _get_notify_script_candidates()

    if any(any(path in str(n) for path in handler_paths) for n in notify):
        print(f"\nNotify hook: configured")
    else:
        handler_path = _get_notify_script_path()
        print(f"\nNotify hook: NOT configured")
        print(f"  Add to ~/.codex/config.toml: notify = [\"{handler_path}\"]")


def main():
    parser = argparse.ArgumentParser(prog="codex-matrix", description="Codex CLI Matrix bridge")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("enable", help="Enable the Codex Matrix bridge")
    sub.add_parser("disable", help="Disable the Codex Matrix bridge")
    sub.add_parser("start", help="Start the Codex daemon")
    sub.add_parser("stop", help="Stop the Codex daemon")
    sub.add_parser("status", help="Show bridge status")

    args = parser.parse_args()

    commands = {
        "enable": cmd_enable,
        "disable": cmd_disable,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
    }

    if args.command in commands:
        sys.exit(commands[args.command](args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
