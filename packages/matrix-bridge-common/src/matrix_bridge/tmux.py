"""tmux integration — send keystrokes to Claude Code sessions."""

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path

import libtmux

logger = logging.getLogger(__name__)

PROC_ROOT = Path("/proc")


def get_server() -> libtmux.Server | None:
    """Get the tmux server, or None if not running."""
    try:
        return libtmux.Server()
    except Exception:
        return None


def find_pane(pane_id: str) -> libtmux.Pane | None:
    """Find a tmux pane by its ID (e.g. '%5')."""
    server = get_server()
    if not server:
        return None

    for session in server.sessions:
        for window in session.windows:
            for pane in window.panes:
                if pane.pane_id == pane_id:
                    return pane
    return None


def pane_current_command(pane_id: str) -> str | None:
    """Return tmux's current command for a pane, or None if unavailable."""
    if not pane_id or pane_id == "unknown":
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    command = result.stdout.strip()
    return command or None


def pid_holding_file(target: Path, proc_root: Path = PROC_ROOT) -> int | None:
    """Return a PID with `target` open, or None.

    The agent CLI keeps its rollout file open for append for the whole life of
    the session, so "who has this file open" is a direct, always-available link
    from a session file back to the process driving it — no cooperation from
    the process required, and unlike file mtime it does not go stale during a
    long tool call.
    """
    try:
        resolved = target.resolve()
    except OSError:
        return None

    for entry in _iter_pids(proc_root):
        fd_dir = proc_root / str(entry) / "fd"
        try:
            handles = list(fd_dir.iterdir())
        except OSError:
            # Processes owned by other users, and ones that exited between the
            # listing and the read, are both normal here.
            continue
        for handle in handles:
            try:
                if handle.resolve() == resolved:
                    return entry
            except OSError:
                continue
    return None


def _iter_pids(proc_root: Path) -> list[int]:
    try:
        return sorted(int(p.name) for p in proc_root.iterdir() if p.name.isdigit())
    except OSError:
        return []


def pane_from_process_env(pid: int, proc_root: Path = PROC_ROOT) -> str | None:
    """Return the TMUX_PANE a process was launched under, or None.

    This is the same value the notify hook reports, read from the authoritative
    source instead of being relayed. It lets the daemon establish a session's
    pane binding on its own, so a provisional `unknown` entry is repaired even
    when the notify hook never fires — a hook that is missing, misconfigured,
    disabled or pinned to a stale plugin version no longer costs the session
    its pane, and therefore no longer feeds it to the staleness reaper.
    """
    try:
        raw = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return None

    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key == b"TMUX_PANE":
            pane = value.decode("utf-8", errors="replace").strip()
            return pane or None
    return None


def pane_for_open_file(target: Path, proc_root: Path = PROC_ROOT) -> str | None:
    """Resolve the tmux pane of whichever process holds `target` open."""
    pid = pid_holding_file(target, proc_root=proc_root)
    if pid is None:
        return None
    return pane_from_process_env(pid, proc_root=proc_root)


def _exit_copy_mode(pane: libtmux.Pane) -> bool:
    """Detect and exit tmux copy mode if active.

    Copy mode captures all keystrokes (including injected ones), so we
    must exit it before sending text to the underlying application.
    The yellow command-prompt (search/jump/repeat) is a sub-state of
    copy mode — Escape cancels the prompt, then 'q' exits copy mode.

    Returns True if copy mode was detected and exited.
    """
    try:
        result = pane.cmd("display-message", "-p", "#{pane_mode}")
        mode = result.stdout[0] if result.stdout else ""
        if mode and mode != "":
            # Cancel any active command-prompt first, then exit copy mode
            pane.cmd("send-keys", "-t", pane.pane_id, "Escape")
            pane.cmd("send-keys", "-t", pane.pane_id, "q")
            logger.info(f"Exited copy mode (was: {mode}) on pane {pane.pane_id}")
            return True
    except Exception as e:
        logger.warning(f"Failed to check/exit copy mode on {pane.pane_id}: {e}")
    return False


async def send_keys(pane_id: str, text: str) -> bool:
    """Send text to a tmux pane as keystrokes, simulating user input.

    For short text (< 500 chars), uses tmux send-keys in literal mode.
    For long text, uses tmux paste-buffer for atomic pasting — send-keys -l
    sends characters one at a time and the TUI can't keep up with long input,
    causing the Enter key to get lost.

    Returns True if successful.
    """
    pane = find_pane(pane_id)
    if not pane:
        logger.error(f"tmux pane {pane_id} not found")
        return False

    try:
        # Exit copy mode if the pane is stuck in it
        if _exit_copy_mode(pane):
            await asyncio.sleep(0.3)

        if len(text) < 500:
            # Short text: send-keys is fine
            pane.send_keys(text, enter=False, literal=True)
        else:
            # Long text: use paste-buffer for atomic paste
            _paste_to_pane(pane_id, text)

        # Wait for TUI to process the text
        await asyncio.sleep(0.5)

        # Press Enter
        pane.send_keys("", enter=True, literal=False)

        logger.info(f"Sent {len(text)} chars to pane {pane_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send keys to {pane_id}: {e}")
        return False


def _paste_to_pane(pane_id: str, text: str) -> None:
    """Paste text into a tmux pane via the paste buffer (atomic operation).

    Uses load-buffer + paste-buffer instead of send-keys -l, which avoids
    character-by-character sending that overwhelms TUIs with long input.
    """
    buf_name = "ccmatrix-paste"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=True) as f:
        f.write(text)
        f.flush()
        subprocess.run(
            ["tmux", "load-buffer", "-b", buf_name, f.name],
            check=True, capture_output=True,
        )
    subprocess.run(
        ["tmux", "paste-buffer", "-b", buf_name, "-t", pane_id, "-d", "-p"],
        check=True, capture_output=True,
    )
