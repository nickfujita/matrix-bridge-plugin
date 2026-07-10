"""tmux integration — send keystrokes to Claude Code sessions."""

import asyncio
import logging
import subprocess
import tempfile

import libtmux

logger = logging.getLogger(__name__)


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
