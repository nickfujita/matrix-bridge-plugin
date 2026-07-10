import asyncio
import unittest
from types import SimpleNamespace

from codex_matrix.daemon import CodexDaemon


class _SessionMapStub:
    def get(self, session_id: str):
        return SimpleNamespace(session_id=session_id, room_id="!room:test", active=True)

    def deregister(self, session_id: str):
        pass


class _BridgeStub:
    def __init__(self):
        self.message_calls: list[tuple[str, list[dict], bool]] = []
        self.typing_calls: list[tuple[str, bool]] = []
        self.ended_calls: list[str] = []

    async def send_messages(self, session_id: str, messages: list[dict], notify_final: bool = False) -> int:
        await asyncio.sleep(0.05)
        self.message_calls.append((session_id, list(messages), notify_final))
        return len(messages)

    async def set_typing(self, session_id: str, typing: bool) -> None:
        self.typing_calls.append((session_id, typing))

    async def mark_session_ended(self, session_id: str) -> None:
        self.ended_calls.append(session_id)


class CodexDaemonTurnCompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_complete_dedupes_overlapping_callbacks(self):
        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.bridge = _BridgeStub()
        daemon.session_map = _SessionMapStub()
        daemon._pending_assistant = {
            "thread-1": [
                {"role": "assistant", "text": "partial"},
                {"role": "assistant", "text": "final"},
            ],
        }
        daemon._last_completed_turn = {}
        daemon._inflight_turns = set()

        await asyncio.gather(
            daemon._on_turn_complete("thread-1", "turn-1"),
            daemon._on_turn_complete("thread-1", "turn-1"),
        )

        self.assertEqual(len(daemon.bridge.message_calls), 1)
        self.assertEqual(len(daemon.bridge.typing_calls), 1)
        # Voice is server-side now: the final message is flushed with
        # notify_final=True (which tags cc.tts in the real bridge); the daemon
        # no longer synthesizes audio itself.
        _sid, messages, notify = daemon.bridge.message_calls[0]
        self.assertTrue(notify)
        self.assertEqual(messages[-1], {"role": "assistant", "text": "final"})
        self.assertEqual(daemon._last_completed_turn, {"thread-1": "turn-1"})
        self.assertEqual(daemon._inflight_turns, set())

    async def test_turn_complete_sends_only_final_for_hidden_automation_session(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.jsonl"
            session_file.write_text(json.dumps({"marker": "PAPER_VOICE_DAILY_RUN_ID=abc"}))

            daemon = CodexDaemon.__new__(CodexDaemon)
            daemon.bridge = _BridgeStub()
            daemon.session_map = _SessionMapStub()
            daemon._pending_assistant = {
                "thread-1": [
                    {"role": "assistant", "text": "thinking out loud"},
                    {"role": "assistant", "text": "final brief"},
                ],
            }
            daemon._last_completed_turn = {}
            daemon._inflight_turns = set()

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file):
                await daemon._on_turn_complete("thread-1", "turn-1")

            self.assertEqual(len(daemon.bridge.message_calls), 1)
            _session_id, messages, notify = daemon.bridge.message_calls[0]
            self.assertTrue(notify)
            self.assertEqual(messages, [{"role": "assistant", "text": "final brief"}])

    async def test_turn_complete_recovers_hidden_final_without_replaying_old_brief(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        def msg(role: str, text: str) -> str:
            block_type = "input_text" if role == "user" else "output_text"
            return json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": role,
                        "content": [{"type": block_type, "text": text}],
                    },
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.jsonl"
            session_file.write_text(
                "\n".join(
                    [
                        msg("assistant", "old brief"),
                        msg("user", "Paper Voice proactive daily check-in.\n\nPAPER_VOICE_DAILY_RUN_ID=abc"),
                        msg("assistant", "new final brief"),
                    ]
                )
                + "\n"
            )

            daemon = CodexDaemon.__new__(CodexDaemon)
            daemon.bridge = _BridgeStub()
            daemon.session_map = _SessionMapStub()
            daemon._pending_assistant = {}
            daemon._last_completed_turn = {}
            daemon._inflight_turns = set()

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file):
                await daemon._on_turn_complete("thread-1", "turn-1")

            self.assertEqual(len(daemon.bridge.message_calls), 1)
            _session_id, messages, notify = daemon.bridge.message_calls[0]
            self.assertTrue(notify)
            self.assertEqual(messages, [{"role": "assistant", "text": "new final brief"}])

    async def test_turn_complete_ignores_subagent_session_without_tts(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            session_file = Path(tmp) / "session.jsonl"
            session_file.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "thread-1",
                            "thread_source": "subagent",
                            "parent_thread_id": "parent-1",
                            "cwd": "/tmp/repo",
                        },
                    }
                )
                + "\n"
            )

            daemon = CodexDaemon.__new__(CodexDaemon)
            daemon.bridge = _BridgeStub()
            daemon.session_map = _SessionMapStub()
            daemon.watched_sessions = {"thread-1"}
            daemon._pending_assistant = {
                "thread-1": [{"role": "assistant", "text": "background result"}],
            }
            daemon._last_completed_turn = {}
            daemon._inflight_turns = set()

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file):
                await daemon._on_turn_complete("thread-1", "turn-1")

            self.assertEqual(daemon.bridge.message_calls, [])
            self.assertEqual(daemon.bridge.ended_calls, ["thread-1"])
            self.assertNotIn("thread-1", daemon.watched_sessions)
