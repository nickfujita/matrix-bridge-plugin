"""CLI entrypoint for setup and management."""

import argparse
import sys
from pathlib import Path

from matrix_bridge.config import MatrixConfig, save_config, load_config, STATE_DIR


def cmd_setup(args):
    """Interactive setup — configure Matrix connection."""
    print("Claude Code Matrix — Setup")
    print("=" * 40)
    print()

    homeserver = input("Matrix homeserver URL (e.g. https://matrix.example.com): ").strip()
    user_id = input("Bot user ID (e.g. @ccbot:example.com): ").strip()
    access_token = input("Bot access token: ").strip()
    admin_user_id = input("Admin user ID to invite (e.g. @admin:example.com): ").strip()

    config = MatrixConfig(
        homeserver=homeserver,
        user_id=user_id,
        access_token=access_token,
        admin_user_id=admin_user_id,
    )
    save_config(config)
    print(f"\nConfig saved to {STATE_DIR / 'config.json'}")


def cmd_status(args):
    """Show daemon and session status."""
    config = load_config()
    if not config:
        print("Not configured. Run: ccmatrix setup")
        return

    print(f"Homeserver:     {config.homeserver}")
    print(f"Bot user:       {config.user_id}")
    print(f"Admin user:     {config.admin_user_id}")
    print()

    # Check daemon
    pid_file = STATE_DIR / "daemon.pid"
    if pid_file.exists():
        import os
        pid = int(pid_file.read_text().strip())
        try:
            os.kill(pid, 0)
            print(f"Daemon: running (PID {pid})")
        except OSError:
            print("Daemon: not running (stale PID file)")
    else:
        print("Daemon: not running")

    # Show active sessions
    from matrix_bridge.session import SessionMap
    session_map = SessionMap(STATE_DIR / "sessions.json")
    active = session_map.active_sessions()
    print(f"\nActive sessions: {len(active)}")
    for entry in active:
        project = Path(entry.cwd).name if entry.cwd else "?"
        room = entry.room_id or "no room"
        print(f"  {entry.session_id[:8]}... | pane {entry.tmux_pane} | {project} | {room}")


def cmd_stop(args):
    """Stop the inbound daemon."""
    pid_file = STATE_DIR / "daemon.pid"
    if not pid_file.exists():
        print("Daemon not running.")
        return

    import os
    import signal
    pid = int(pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (PID {pid})")
    except OSError as e:
        print(f"Failed to stop daemon: {e}")
        pid_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(prog="ccmatrix", description="Claude Code Matrix bridge")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Configure Matrix connection")
    sub.add_parser("status", help="Show bridge status")
    sub.add_parser("stop", help="Stop the inbound daemon")

    args = parser.parse_args()

    commands = {
        "setup": cmd_setup,
        "status": cmd_status,
        "stop": cmd_stop,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
