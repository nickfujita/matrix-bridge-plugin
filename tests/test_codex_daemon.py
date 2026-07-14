import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_matrix.bridge import CodexBridge
from codex_matrix.daemon import CodexDaemon
from matrix_bridge.session import SessionMap


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


class _WatcherStub:
    def __init__(self, events: list[tuple]):
        self.events = events
        self.watched_files: list[Path] = []

    def watch_file(self, path: Path) -> None:
        self.watched_files.append(path)
        self.events.append(("watch_file", path))


class _MatrixClientStub:
    def __init__(self, events: list[tuple], room_names: dict[str, str]):
        self.events = events
        self.room_names = room_names

    async def room_set_name(self, room_id: str, name: str) -> None:
        self.room_names[room_id] = name
        self.events.append(("room_set_name", room_id, name))

    async def room_send(
        self,
        room_id: str,
        text: str,
        catchup: bool = False,
        tts: bool = False,
        raise_on_unavailable: bool = False,
    ) -> None:
        self.events.append(("room_send", room_id, text, catchup, tts, raise_on_unavailable))

    async def room_typing(self, room_id: str, user_id: str, typing: bool, timeout: int) -> None:
        self.events.append(("room_typing", room_id, user_id, typing, timeout))


class _RecordingCodexBridge(CodexBridge):
    def __init__(self, session_map: SessionMap, bot_client: _MatrixClientStub):
        self.config = SimpleNamespace(
            repo_aliases={},
            server_side_voice=True,
            user_id="@codex:test",
        )
        self.session_map = session_map
        self.bot_client = bot_client
        self.message_calls: list[tuple[str, list[dict], bool]] = []

    async def send_messages(self, session_id: str, messages: list[dict], notify_final: bool = False) -> int:
        self.message_calls.append((session_id, list(messages), notify_final))
        return await super().send_messages(session_id, messages, notify_final=notify_final)


class CodexDaemonSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_resumed_session_setup_then_completion_restores_room_and_sends_fallback_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_file = root / "rollout-thread-1.jsonl"
            session_file.write_text("")

            session_map = SessionMap(root / "codex-sessions.json")
            session_map.register("thread-1", "%old", "/workspace/project")
            session_map.set_room_id("thread-1", "!room:test")
            session_map.set_last_branch("thread-1", "main")
            session_map.deregister("thread-1")

            # The notify handler reactivates the mapping before signaling the
            # daemon. Room and branch history survive that transition.
            session_map.register("thread-1", "%new", "/workspace/project")
            resumed_entry = session_map.get("thread-1")
            self.assertTrue(resumed_entry.active)
            self.assertEqual(resumed_entry.room_id, "!room:test")
            self.assertEqual(resumed_entry.last_branch, "main")

            events: list[tuple] = []
            room_names = {"!room:test": "🔴 project/main"}
            matrix_client = _MatrixClientStub(events, room_names)
            bridge = _RecordingCodexBridge(session_map, matrix_client)

            daemon = CodexDaemon.__new__(CodexDaemon)
            daemon.bridge = bridge
            daemon.session_map = session_map
            daemon.watcher = _WatcherStub(events)
            daemon.watched_sessions = set()
            daemon._pending_assistant = {}
            daemon._last_completed_turn = {}
            daemon._inflight_turns = set()

            signal = {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "cwd": "/workspace/project",
                "tmux_pane": "%new",
                "last_assistant_message": "Recovered final response",
            }

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="main"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                await daemon._handle_notify_signal(signal)
                await daemon._handle_notify_signal(signal)

            self.assertEqual(daemon.watcher.watched_files, [session_file])
            self.assertEqual(session_map.get("thread-1").room_id, "!room:test")
            self.assertEqual(session_map.get("thread-1").last_branch, "main")
            self.assertEqual(room_names["!room:test"], "project/main")
            self.assertEqual(
                bridge.message_calls,
                [
                    (
                        "thread-1",
                        [{"role": "assistant", "text": "Recovered final response"}],
                        True,
                    )
                ],
            )
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("room_set_name"), event_names.index("watch_file"))
            self.assertLess(event_names.index("watch_file"), event_names.index("room_send"))
            self.assertEqual(daemon._last_completed_turn, {"thread-1": "turn-1"})
            self.assertEqual(daemon._inflight_turns, set())


class CodexDaemonTurnCompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_complete_appends_distinct_notify_fallback_to_buffer(self):
        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.bridge = _BridgeStub()
        daemon.session_map = _SessionMapStub()
        daemon._pending_assistant = {
            "thread-1": [{"role": "assistant", "text": "commentary"}],
        }
        daemon._last_completed_turn = {}
        daemon._inflight_turns = set()

        await daemon._on_turn_complete("thread-1", "turn-1", "final response")

        self.assertEqual(
            daemon.bridge.message_calls,
            [
                (
                    "thread-1",
                    [
                        {"role": "assistant", "text": "commentary"},
                        {"role": "assistant", "text": "final response"},
                    ],
                    True,
                )
            ],
        )

    async def test_turn_complete_does_not_duplicate_matching_notify_fallback(self):
        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.bridge = _BridgeStub()
        daemon.session_map = _SessionMapStub()
        daemon._pending_assistant = {
            "thread-1": [{"role": "assistant", "text": "final response"}],
        }
        daemon._last_completed_turn = {}
        daemon._inflight_turns = set()

        await daemon._on_turn_complete("thread-1", "turn-1", "final response")

        self.assertEqual(
            daemon.bridge.message_calls,
            [
                (
                    "thread-1",
                    [{"role": "assistant", "text": "final response"}],
                    True,
                )
            ],
        )

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
