"""CLI entrypoint for Antigravity Matrix bridge management."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from matrix_bridge.config import STATE_DIR, load_config
from matrix_bridge.session import SessionMap

ANTIGRAVITY_STATE_DIR = STATE_DIR
ENABLED_FLAG = ANTIGRAVITY_STATE_DIR / "antigravity-enabled"
PLUGIN_NAME = "ccmatrix-antigravity"
PLUGIN_SOURCE_DIR = ANTIGRAVITY_STATE_DIR / "antigravity-plugin"


def cmd_enable(args) -> None:
    """Enable the Antigravity Matrix bridge."""
    config = load_config()
    if not config:
        print("Not configured. Run 'ccmatrix setup' first.")
        return

    ANTIGRAVITY_STATE_DIR.mkdir(parents=True, exist_ok=True)
    ENABLED_FLAG.touch()
    print("Antigravity Matrix bridge enabled.")

    _install_antigravity_plugin()
    _sync_antigravity_skills()
    cmd_start(args)


def cmd_disable(args) -> None:
    """Disable the Antigravity Matrix bridge."""
    ENABLED_FLAG.unlink(missing_ok=True)
    _disable_antigravity_plugin()
    cmd_stop(args)
    print("Antigravity Matrix bridge disabled.")


def cmd_start(args) -> None:
    """Start the Antigravity daemon."""
    pid_file = ANTIGRAVITY_STATE_DIR / "antigravity-daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"Antigravity daemon already running (PID {pid})")
            return
        except (OSError, ValueError):
            pass

    proc = subprocess.Popen(
        [sys.executable, "-m", "antigravity_matrix"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"Antigravity daemon started (PID {proc.pid})")


def cmd_stop(args) -> None:
    """Stop the Antigravity daemon."""
    pid_file = ANTIGRAVITY_STATE_DIR / "antigravity-daemon.pid"
    if not pid_file.exists():
        print("Antigravity daemon not running.")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to Antigravity daemon (PID {pid})")
    except (OSError, ValueError) as exc:
        print(f"Failed to stop daemon: {exc}")
        pid_file.unlink(missing_ok=True)


def cmd_status(args) -> None:
    """Show Antigravity bridge status."""
    config = load_config()
    if not config:
        print("Not configured. Run 'ccmatrix setup' first.")
        return

    enabled = ENABLED_FLAG.exists()
    print(f"Antigravity bridge: {'enabled' if enabled else 'disabled'}")

    pid_file = ANTIGRAVITY_STATE_DIR / "antigravity-daemon.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"Daemon: running (PID {pid})")
        except (OSError, ValueError):
            print("Daemon: not running (stale PID file)")
    else:
        print("Daemon: not running")

    session_map = SessionMap(ANTIGRAVITY_STATE_DIR / "antigravity-sessions.json")
    active = session_map.active_sessions()
    print(f"\nActive Antigravity sessions: {len(active)}")
    for entry in active:
        project = Path(entry.cwd).name if entry.cwd else "?"
        room = entry.room_id or "no room"
        print(f"  {entry.session_id[:8]}... | pane {entry.tmux_pane} | {project} | {room}")

    _check_antigravity_plugin()


def cmd_install_plugin(args) -> None:
    """Install or refresh the Antigravity plugin only."""
    _install_antigravity_plugin()


def cmd_sync_skills(args) -> None:
    """Sync shared local skills into Antigravity's global skill directory."""
    _sync_antigravity_skills()


def _hook_command(handler: str) -> str:
    return f'"{sys.executable}" -m antigravity_matrix.hooks {handler}'



def _skill_source_dirs() -> list[Path]:
    """Local skill roots to expose to Antigravity.

    Defaults to `~/.agents/skills`. Override or extend by setting
    `CCMATRIX_ANTIGRAVITY_SKILL_DIRS` to a colon-separated list of directories,
    each containing skill folders with a `SKILL.md` (e.g.
    `~/my-project/.claude/skills:~/another/skills`).
    """
    override = os.environ.get("CCMATRIX_ANTIGRAVITY_SKILL_DIRS")
    if override:
        return [Path(p).expanduser() for p in override.split(os.pathsep) if p.strip()]
    return [Path.home() / ".agents" / "skills"]


def _sync_antigravity_skills() -> None:
    """Symlink shared local skills into Antigravity's global skill directory."""
    target_root = Path.home() / ".gemini" / "config" / "skills"
    target_root.mkdir(parents=True, exist_ok=True)

    linked = 0
    skipped = 0
    for source_root in _skill_source_dirs():
        if not source_root.exists():
            continue
        for skill_dir in sorted(p for p in source_root.iterdir() if p.is_dir()):
            if not (skill_dir / "SKILL.md").exists():
                continue
            target = target_root / skill_dir.name
            if target.is_symlink():
                if target.resolve() == skill_dir.resolve():
                    skipped += 1
                    continue
                target.unlink()
            elif target.exists():
                print(f"Skipping existing Antigravity skill {target.name}; target already exists.")
                skipped += 1
                continue
            target.symlink_to(skill_dir, target_is_directory=True)
            linked += 1

    print(f"Antigravity skills synced: {linked} linked, {skipped} already present or skipped.")

def _write_plugin_source() -> None:
    PLUGIN_SOURCE_DIR.mkdir(parents=True, exist_ok=True)

    (PLUGIN_SOURCE_DIR / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": "https://antigravity.google/schemas/v1/plugin.json",
                "name": PLUGIN_NAME,
                "description": "Matrix and TTS bridge hooks for Google Antigravity CLI.",
            },
            indent=2,
        )
        + "\n"
    )

    hooks = {
        "matrix-bridge": {
            "PreInvocation": [
                {"type": "command", "command": _hook_command("pre_invocation"), "timeout": 30}
            ],
            "PostInvocation": [
                {"type": "command", "command": _hook_command("post_invocation"), "timeout": 30}
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": _hook_command("post_tool_use"), "timeout": 30}
                    ],
                }
            ],
            "Stop": [
                {"type": "command", "command": _hook_command("stop"), "timeout": 600}
            ],
        }
    }
    (PLUGIN_SOURCE_DIR / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n")


def _run_agy_plugin(*args: str) -> subprocess.CompletedProcess[str] | None:
    agy = shutil.which("agy")
    if not agy:
        print("Warning: agy command not found. Install Antigravity CLI first.")
        return None
    return subprocess.run(
        [agy, "plugin", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _install_antigravity_plugin() -> None:
    """Create and install the Antigravity plugin for hook integration."""
    _write_plugin_source()

    result = _run_agy_plugin("install", str(PLUGIN_SOURCE_DIR))
    if result is None:
        print(f"Plugin files written to {PLUGIN_SOURCE_DIR}")
        return

    if result.returncode == 0:
        print("Antigravity plugin installed.")
        if result.stdout.strip():
            print(result.stdout.strip())
        return

    print("Warning: agy plugin install failed.")
    if result.stderr.strip():
        print(result.stderr.strip())
    if result.stdout.strip():
        print(result.stdout.strip())


def _disable_antigravity_plugin() -> None:
    result = _run_agy_plugin("disable", PLUGIN_NAME)
    if result and result.returncode == 0:
        print("Antigravity plugin disabled.")


def _check_antigravity_plugin() -> None:
    result = _run_agy_plugin("list")
    if result is None:
        print("\nAntigravity plugin: agy command not found")
        return

    output = (result.stdout or "") + (result.stderr or "")
    if PLUGIN_NAME in output:
        print("\nAntigravity plugin: installed")
    else:
        print("\nAntigravity plugin: NOT installed")
        print("  Run: antigravity-matrix install-plugin")


def main() -> None:
    parser = argparse.ArgumentParser(prog="antigravity-matrix", description="Antigravity CLI Matrix bridge")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("enable", help="Enable the Antigravity Matrix bridge")
    sub.add_parser("disable", help="Disable the Antigravity Matrix bridge")
    sub.add_parser("start", help="Start the Antigravity daemon")
    sub.add_parser("stop", help="Stop the Antigravity daemon")
    sub.add_parser("status", help="Show bridge status")
    sub.add_parser("install-plugin", help="Install or refresh only the Antigravity plugin")
    sub.add_parser("sync-skills", help="Expose shared local skills to Antigravity")

    args = parser.parse_args()

    commands = {
        "enable": cmd_enable,
        "disable": cmd_disable,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "install-plugin": cmd_install_plugin,
        "sync-skills": cmd_sync_skills,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
