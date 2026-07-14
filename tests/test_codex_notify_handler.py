import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_matrix import notify_handler


class CodexNotifyHandlerTests(unittest.TestCase):
    def test_signal_preserves_last_assistant_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            enabled_flag.touch()
            session_file = state_dir / "rollout-thread-1.jsonl"
            payload = {
                "type": "agent-turn-complete",
                "thread-id": "thread-1",
                "turn-id": "turn-1",
                "cwd": "/workspace/project",
                "last-assistant-message": "Distinct completed response",
            }

            with (
                patch.object(notify_handler, "STATE_DIR", state_dir),
                patch.object(notify_handler, "ENABLED_FLAG", enabled_flag),
                patch.object(sys, "argv", ["codex-matrix-notify", json.dumps(payload)]),
                patch.dict(os.environ, {"TMUX_PANE": "%7"}),
                patch.object(notify_handler, "find_session_file", return_value=session_file),
                patch.object(notify_handler, "is_unmirrored_session_meta", return_value=False),
                patch.object(notify_handler, "is_unmirrored_session", return_value=False),
                patch.object(notify_handler, "_ensure_daemon_running") as ensure_daemon,
            ):
                notify_handler.handle_notify()

            signal = json.loads((state_dir / "codex-notify-signal").read_text())
            self.assertEqual(
                signal,
                {
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                    "cwd": "/workspace/project",
                    "tmux_pane": "%7",
                    "last_assistant_message": "Distinct completed response",
                },
            )
            ensure_daemon.assert_called_once_with()
