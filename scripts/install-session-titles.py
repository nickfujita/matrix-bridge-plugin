#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# ///
"""Install session titles for the current user's default tmux server."""

from datetime import datetime, timezone
import argparse
from pathlib import Path
import os
import re
import shlex
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
USER_HOME = Path.home()
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
START = "# session-titles:start"
END = "# session-titles:end"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text() == text:
            return
        shutil.copy2(path, path.with_name(path.name + ".pre-session-titles-" + STAMP))
    path.write_text(text)


def section(path, body):
    old = path.read_text() if path.exists() else ""
    block = START + "\n" + body.strip() + "\n" + END
    if START in old:
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, old, flags=re.S)
    else:
        text = old.rstrip() + "\n\n" + block + "\n"
    write(path, text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adopt-windows", action="store_true", help="Enable automatic names on existing agent windows")
    args = parser.parse_args()
    runtime_dir = Path(f"/run/user/{os.getuid()}")
    if runtime_dir.is_dir():
        os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime_dir))
        if (runtime_dir / "bus").exists():
            os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")
    subprocess.run(["systemctl", "--user", "show-environment"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["uv", "sync", "--all-packages", "--project", str(ROOT)], check=True)
    wrapper = USER_HOME / ".local/bin/session-title"
    uv = shutil.which("uv")
    write(wrapper, f'#!/bin/sh\nexec {shlex.quote(uv)} run --no-sync --quiet --project {shlex.quote(str(ROOT))} session-title "$@"\n')
    wrapper.chmod(0o755)

    # Read only to validate TOML. Preserve comments and unrelated configuration.
    config = Path(os.environ.get("CODEX_HOME") or USER_HOME / ".codex") / "config.toml"
    text = config.read_text() if config.exists() else ""
    tomllib.loads(text)
    header = re.search(r"(?m)^\[tui\]\s*$", text)
    setting = 'terminal_title = ["thread"]\n'
    if header:
        tail = text[header.end():]
        next_table = re.search(r"(?m)^\[", tail)
        end = header.end() + next_table.start() if next_table else len(text)
        body = text[header.end():end]
        # Existing multiline values need a TOML-aware edit, not a guessed splice.
        match = re.search(r"(?m)^terminal_title\s*=.*$", body)
        if match and not re.fullmatch(r"terminal_title\s*=\s*\[.*\]\s*(?:#.*)?", match.group()):
            raise SystemExit("Edit multiline tui.terminal_title to [\"thread\"] before rerunning")
        body = re.sub(r"(?m)^terminal_title\s*=.*\n?", "", body)
        text = text[:header.end()] + "\n" + setting + body.lstrip("\n") + text[end:]
    else:
        text = text.rstrip() + "\n\n[tui]\n" + setting
    tomllib.loads(text)
    write(config, text)

    # Values stored in pane options are expanded once; never use E: or #() on
    # title text. Manual rename-window keeps tmux's normal per-window override.
    is_agent = "#{||:#{==:#{pane_current_command},codex},#{==:#{pane_current_command},claude}}"
    title = "#{?@session_title,#{@session_title},#{pane_title}}"
    repo = "#{?@session_repo,#{@session_repo},#{b:pane_current_path}}"
    short = "#{?@session_title,#{=/28/…:@session_title},#{=/28/…:pane_title}}"
    section(USER_HOME / ".tmux.conf.local", f'''
set -gw automatic-rename on
set -gw automatic-rename-format '#{{?{is_agent},{short},#{{pane_current_command}}}}'
set -g status-right-length 80
set -g status-right ' #[fg=#dba3c4]#{{?{is_agent},{repo} #[fg=#666666]| #[fg=#dba3c4]{title},#{{pane_current_path}}}} '
''')
    section(USER_HOME / ".codex/AGENTS.md", '''
## Session titles

Keep the session title a short description of the overarching objective, usually
3 to 7 words. When the user replaces that objective or a sustained change makes
the title misleading, run `session-title set "New title"`. Use `session-title get`
to check the current title. Do not change it on every turn, branch change, review,
test run, or debugging detour. Preserve a title the user explicitly chose unless
they ask to change it. Only the main session updates its title. Subagents must
not call the helper or rename their parent session. Do not inject `/rename` into
a running terminal. The helper updates native metadata; tmux and Matrix follow it.
If the helper cannot identify the session, do not guess from the working directory.
''')
    unit = USER_HOME / ".config/systemd/user/session-titles.service"
    write(unit, f'''[Unit]
Description=Sync native agent session titles to tmux and Matrix

[Service]
ExecStart="{wrapper}" watch
Environment="PATH={USER_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
''')
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "session-titles.service"], check=True)
    subprocess.run(["systemctl", "--user", "restart", "session-titles.service"], check=True)
    # No server is also a valid installation state. The next launch sources it.
    result = subprocess.run(["tmux", "source-file", str(USER_HOME / ".tmux.conf.local")], capture_output=True, text=True)
    if result.returncode:
        print("tmux settings saved; load them when the default server starts.")
    elif args.adopt_windows:
        result = subprocess.run(["tmux", "list-windows", "-a", "-F", "#{window_id}\t#{pane_current_command}\t#{window_name}\t#{automatic-rename}"],
                                check=True, capture_output=True, text=True)
        write(USER_HOME / ".ccmatrix" / ("tmux-windows-before-titles-" + STAMP + ".tsv"), result.stdout)
        for row in result.stdout.splitlines():
            window, command, *_ = row.split("\t")
            if command in ("codex", "claude"):
                subprocess.run(["tmux", "set-option", "-w", "-t", window, "automatic-rename", "on"], check=True)
    print("Installed session-title, tmux formats, Codex title settings, global guidance, and user service.")
    print("Existing manually named tmux windows keep their override. Re-enable automatic-rename per window to follow chat titles.")


if __name__ == "__main__":
    main()
