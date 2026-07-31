"""CLI entrypoint for Codex Matrix bridge management."""

import argparse
import json
import os
import re
import signal
import shlex
import sys
from pathlib import Path

from matrix_bridge.config import load_config, STATE_DIR


CODEX_STATE_DIR = STATE_DIR  # ~/.ccmatrix
ENABLED_FLAG = CODEX_STATE_DIR / "codex-enabled"

# The generated wrapper records the passthrough notifier here so a later
# `codex-matrix enable` can recover it without parsing shell.
_PASSTHROUGH_MARKER = "# matrix-bridge:passthrough "


def cmd_enable(args):
    """Enable the Codex Matrix bridge."""
    config = load_config()
    if not config:
        print("Not configured. Run 'ccmatrix setup' first.")
        return

    CODEX_STATE_DIR.mkdir(parents=True, exist_ok=True)
    ENABLED_FLAG.touch()
    print("Codex Matrix bridge enabled.")

    # Install notify hook in Codex config
    _install_notify_hook()

    # Start daemon
    cmd_start(args)


def cmd_disable(args):
    """Disable the Codex Matrix bridge."""
    ENABLED_FLAG.unlink(missing_ok=True)
    cmd_stop(args)
    print("Codex Matrix bridge disabled.")


def cmd_start(args):
    """Start the Codex daemon."""
    pid_file = CODEX_STATE_DIR / "codex-daemon.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Codex daemon already running (PID {pid})")
            return
        except OSError:
            pass

    import subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "codex_matrix"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Codex daemon started (PID {proc.pid})")


def cmd_stop(args):
    """Stop the Codex daemon."""
    pid_file = CODEX_STATE_DIR / "codex-daemon.pid"
    if not pid_file.exists():
        print("Codex daemon not running.")
        return

    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Codex daemon (PID {pid})")
    except OSError as e:
        print(f"Failed to stop daemon: {e}")
        pid_file.unlink(missing_ok=True)


def cmd_status(args):
    """Show Codex bridge status."""
    config = load_config()
    if not config:
        print("Not configured. Run 'ccmatrix setup' first.")
        return

    enabled = ENABLED_FLAG.exists()
    print(f"Codex bridge: {'enabled' if enabled else 'disabled'}")

    pid_file = CODEX_STATE_DIR / "codex-daemon.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Daemon: running (PID {pid})")
        except OSError:
            print("Daemon: not running (stale PID file)")
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


def _install_notify_hook():
    """Install the Codex notify fan-out wrapper in Codex config.toml."""
    import tomllib

    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        print("Warning: ~/.codex/config.toml not found. Codex may not be installed.")
        return

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    notify = config.get("notify", [])
    wrapper_path = _get_notify_wrapper_path()
    passthrough_command = _existing_notify_command(notify)
    if _notify_points_to_wrapper(notify):
        passthrough_command = _existing_wrapper_passthrough_command() or passthrough_command
    _write_notify_scripts(passthrough_command)

    if _notify_points_to_wrapper(notify):
        print("Notify hook already configured.")
        return

    content = config_path.read_text()
    notify_line = f'notify = [ "{wrapper_path}" ]'

    if re.search(r"(?m)^notify\s*=\s*\[[^\n]*\]\s*$", content):
        content = re.sub(r"(?m)^notify\s*=\s*\[[^\n]*\]\s*$", notify_line, content, count=1)
    elif "notify" not in content:
        content += f"\n{notify_line}\n"
    else:
        print("Warning: 'notify' exists in config.toml but is not a single-line array.")
        print(f"Please manually set it to: {notify_line}")
        return

    config_path.write_text(content)
    print("Added notify hook to ~/.codex/config.toml")


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


def _write_notify_scripts(passthrough_command: str | None) -> None:
    """Write the Matrix notify handler and fan-out wrapper scripts."""
    state_dir = Path.home() / ".ccmatrix"
    state_dir.mkdir(parents=True, exist_ok=True)

    project_root = Path(__file__).resolve().parents[4]
    matrix_notify = Path(_get_notify_script_path())
    wrapper = Path(_get_notify_wrapper_path())

    matrix_notify.write_text(
        "#!/bin/bash\n"
        "# Codex Matrix bridge notify handler - called by Codex on agent-turn-complete.\n"
        'echo "$(date) notify called with: ${1:0:200}" >> '
        f"{shlex.quote(str(state_dir / 'codex-notify.log'))}\n"
        f"cd {shlex.quote(str(project_root))} || exit 1\n"
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
            f'  "$matrix_passthrough" {extra_args}"$@" '
            f">> {shlex.quote(str(state_dir / 'codex-notify-wrapper.log'))} 2>&1 || true",
            "fi",
            "",
        ]
    wrapper_lines.append(
        f"{shlex.quote(str(matrix_notify))} \"$@\" >> {shlex.quote(str(state_dir / 'codex-notify-wrapper.log'))} 2>&1 || true"
    )
    wrapper.write_text("\n".join(wrapper_lines) + "\n")

    matrix_notify.chmod(0o755)
    wrapper.chmod(0o755)


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
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
