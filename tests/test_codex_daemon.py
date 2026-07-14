import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_matrix.bridge import CodexBridge
from codex_matrix.daemon import CodexDaemon
from codex_matrix.watcher import SessionFileHandler
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


class _RecordingSessionFileHandler(SessionFileHandler):
    def __init__(self, events: list[tuple], loop: asyncio.AbstractEventLoop):
        async def unused_callback(_path: Path, _messages: list[dict]) -> None:
            pass

        super().__init__(unused_callback, loop)
        self.events = events

    def watch_file(self, path: Path) -> None:
        super().watch_file(path)
        self.events.append(("watch_file", path))


class _MatrixClientStub:
    def __init__(
        self,
        events: list[tuple],
        room_names: dict[str, str],
        *,
        room_set_name_result: bool = True,
        room_set_name_error: Exception | None = None,
        room_set_name_started: asyncio.Event | None = None,
        room_set_name_release: asyncio.Event | None = None,
        active_room_set_name_started: asyncio.Event | None = None,
        active_room_set_name_release: asyncio.Event | None = None,
    ):
        self.events = events
        self.room_names = room_names
        self.room_set_name_result = room_set_name_result
        self.room_set_name_error = room_set_name_error
        self.room_set_name_started = room_set_name_started
        self.room_set_name_release = room_set_name_release
        self.active_room_set_name_started = active_room_set_name_started
        self.active_room_set_name_release = active_room_set_name_release

    async def room_set_name(self, room_id: str, name: str) -> bool:
        self.events.append(("room_set_name", room_id, name))
        if self.room_set_name_started:
            self.room_set_name_started.set()
        if self.room_set_name_release:
            await self.room_set_name_release.wait()
        if not name.startswith("🔴"):
            if self.active_room_set_name_started:
                self.active_room_set_name_started.set()
            if self.active_room_set_name_release:
                await self.active_room_set_name_release.wait()
        if self.room_set_name_error:
            raise self.room_set_name_error
        if self.room_set_name_result:
            self.room_names[room_id] = name
        return self.room_set_name_result

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
    def _build_resumed_daemon(
        self,
        root: Path,
        *,
        last_branch: str = "main",
        room_set_name_result: bool = True,
        room_set_name_error: Exception | None = None,
        active_room_set_name_started: asyncio.Event | None = None,
        active_room_set_name_release: asyncio.Event | None = None,
    ) -> tuple[
        CodexDaemon,
        _RecordingCodexBridge,
        SessionMap,
        _RecordingSessionFileHandler,
        _MatrixClientStub,
        Path,
        list[tuple],
    ]:
        session_file = root / "rollout-thread-1.jsonl"
        session_file.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "Recovered final response"}
                        ],
                    },
                }
            )
            + "\n"
        )

        session_map = SessionMap(root / "codex-sessions.json")
        session_map.register("thread-1", "%old", "/workspace/project")
        session_map.set_room_id("thread-1", "!room:test")
        session_map.set_last_branch("thread-1", last_branch)
        session_map.deregister("thread-1")

        # The notify handler reactivates the mapping before signaling the
        # daemon. Room and branch history survive that transition.
        session_map.register("thread-1", "%new", "/workspace/project")

        events: list[tuple] = []
        room_names = {"!room:test": "🔴 project/main"}
        matrix_client = _MatrixClientStub(
            events,
            room_names,
            room_set_name_result=room_set_name_result,
            room_set_name_error=room_set_name_error,
            active_room_set_name_started=active_room_set_name_started,
            active_room_set_name_release=active_room_set_name_release,
        )
        bridge = _RecordingCodexBridge(session_map, matrix_client)

        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.bridge = bridge
        daemon.session_map = session_map
        watcher = _RecordingSessionFileHandler(events, asyncio.get_running_loop())
        daemon.watcher = watcher
        daemon.watched_sessions = set()
        daemon._pending_assistant = {}
        daemon._last_completed_turn = {}
        daemon._inflight_turns = set()
        daemon._decoration_tasks = set()
        daemon._active_title_tasks = {}
        daemon._title_locks = {}

        return daemon, bridge, session_map, watcher, matrix_client, session_file, events

    async def _finish_decorations(self, daemon: CodexDaemon) -> None:
        tasks = list(daemon._decoration_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def test_resumed_session_setup_then_completion_restores_room_and_sends_fallback_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                daemon,
                bridge,
                session_map,
                watcher,
                matrix_client,
                session_file,
                events,
            ) = self._build_resumed_daemon(root)
            resumed_entry = session_map.get("thread-1")
            self.assertTrue(resumed_entry.active)
            self.assertEqual(resumed_entry.room_id, "!room:test")
            self.assertEqual(resumed_entry.last_branch, "main")

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
                await self._finish_decorations(daemon)

            self.assertEqual(watcher.offsets[str(session_file)], session_file.stat().st_size)
            self.assertGreater(watcher.offsets[str(session_file)], 0)
            self.assertEqual(daemon._pending_assistant, {})
            self.assertEqual(session_map.get("thread-1").room_id, "!room:test")
            self.assertEqual(session_map.get("thread-1").last_branch, "main")
            self.assertEqual(matrix_client.room_names["!room:test"], "project/main")
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
            self.assertLess(event_names.index("watch_file"), event_names.index("room_send"))
            self.assertLess(event_names.index("room_send"), event_names.index("room_set_name"))
            self.assertEqual(daemon._last_completed_turn, {"thread-1": "turn-1"})
            self.assertEqual(daemon._inflight_turns, set())

    async def test_resumed_completion_survives_active_title_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                daemon,
                bridge,
                _session_map,
                watcher,
                _matrix_client,
                session_file,
                events,
            ) = self._build_resumed_daemon(
                root,
                room_set_name_error=RuntimeError("Matrix rename failed"),
            )
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
                self.assertLogs("codex_matrix.daemon", level="WARNING") as captured,
            ):
                await daemon._handle_notify_signal(signal)
                await self._finish_decorations(daemon)

            self.assertEqual(watcher.offsets[str(session_file)], session_file.stat().st_size)
            self.assertIn("thread-1", daemon.watched_sessions)
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
            self.assertTrue(any("active room title" in message for message in captured.output))
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("watch_file"), event_names.index("room_send"))
            self.assertLess(event_names.index("room_send"), event_names.index("room_set_name"))

    async def test_resumed_completion_survives_active_title_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                daemon,
                bridge,
                session_map,
                watcher,
                matrix_client,
                session_file,
                events,
            ) = self._build_resumed_daemon(
                root,
                last_branch="stale-branch",
                room_set_name_result=False,
            )
            signal = {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "cwd": "/workspace/project",
                "tmux_pane": "%new",
                "last_assistant_message": "Recovered final response",
            }

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
                self.assertLogs("codex_matrix.daemon", level="WARNING") as captured,
            ):
                await daemon._handle_notify_signal(signal)
                await self._finish_decorations(daemon)

            self.assertEqual(watcher.offsets[str(session_file)], session_file.stat().st_size)
            self.assertIn("thread-1", daemon.watched_sessions)
            self.assertEqual(session_map.get("thread-1").last_branch, "stale-branch")
            self.assertEqual(matrix_client.room_names["!room:test"], "🔴 project/main")
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
            self.assertTrue(any("active room title" in message for message in captured.output))
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("watch_file"), event_names.index("room_send"))
            self.assertLess(event_names.index("room_send"), event_names.index("room_set_name"))

    async def test_cold_start_discovery_restores_active_title_after_attaching_watcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                daemon,
                _bridge,
                _session_map,
                watcher,
                matrix_client,
                session_file,
                events,
            ) = self._build_resumed_daemon(root)

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="main"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                await daemon._discover_sessions()
                await self._finish_decorations(daemon)

            self.assertIn("thread-1", daemon.watched_sessions)
            self.assertEqual(watcher.offsets[str(session_file)], session_file.stat().st_size)
            self.assertEqual(matrix_client.room_names["!room:test"], "project/main")
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("watch_file"), event_names.index("room_set_name"))

    async def test_retirement_waits_for_inflight_active_title_before_marking_room_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_rename_started = asyncio.Event()
            release_active_rename = asyncio.Event()
            (
                daemon,
                _bridge,
                session_map,
                _watcher,
                matrix_client,
                _session_file,
                events,
            ) = self._build_resumed_daemon(
                Path(tmp),
                active_room_set_name_started=active_rename_started,
                active_room_set_name_release=release_active_rename,
            )

            with (
                patch("codex_matrix.bridge.detect_branch", return_value="main"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                daemon.watched_sessions.add("thread-1")
                active_title_task = daemon._schedule_active_title_restore("thread-1")
                daemon._schedule_active_title_restore("thread-1")
                await asyncio.wait_for(active_rename_started.wait(), timeout=1)
                retire_task = asyncio.create_task(
                    daemon._retire_session("thread-1", "test retirement")
                )
                try:
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(retire_task), timeout=0.01)
                finally:
                    release_active_rename.set()
                await asyncio.gather(active_title_task, retire_task)

            entry = session_map.get("thread-1")
            self.assertFalse(active_title_task.cancelled())
            self.assertFalse(entry.active)
            self.assertEqual(matrix_client.room_names["!room:test"], "🔴 project/main")
            title_events = [event for event in events if event[0] == "room_set_name"]
            self.assertEqual(
                [event[2] for event in title_events],
                ["project/main", "🔴 project/main"],
            )

    async def test_failed_setup_does_not_restore_title_without_watcher_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            (
                daemon,
                bridge,
                _session_map,
                _watcher,
                matrix_client,
                _session_file,
                events,
            ) = self._build_resumed_daemon(Path(tmp))
            signal = {
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "cwd": "/workspace/project",
                "tmux_pane": "%new",
                "last_assistant_message": "Recovered final response",
            }

            with patch("codex_matrix.daemon.find_session_file", return_value=None):
                await daemon._handle_notify_signal(signal)
                await self._finish_decorations(daemon)

            self.assertNotIn("thread-1", daemon.watched_sessions)
            self.assertEqual(matrix_client.room_names["!room:test"], "🔴 project/main")
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
            self.assertTrue(any(event[0] == "room_send" for event in events))
            self.assertFalse(any(event[0] == "room_set_name" for event in events))

    async def test_mark_session_active_reports_rejected_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (
                _daemon,
                bridge,
                session_map,
                _watcher,
                _matrix_client,
                _session_file,
                _events,
            ) = self._build_resumed_daemon(
                root,
                last_branch="stale-branch",
                room_set_name_result=False,
            )

            with (
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                renamed = await bridge.mark_session_active("thread-1")

            self.assertFalse(renamed)
            self.assertEqual(session_map.get("thread-1").last_branch, "stale-branch")


class CodexDaemonBranchRefreshTests(unittest.IsolatedAsyncioTestCase):
    thread_id = "00000000-0000-0000-0000-000000000001"

    def _build_watched_daemon(
        self,
        root: Path,
        *,
        room_set_name_result: bool = True,
        room_set_name_error: Exception | None = None,
        room_set_name_started: asyncio.Event | None = None,
        room_set_name_release: asyncio.Event | None = None,
        active_room_set_name_started: asyncio.Event | None = None,
        active_room_set_name_release: asyncio.Event | None = None,
    ) -> tuple[CodexDaemon, _RecordingCodexBridge, SessionMap, _MatrixClientStub, Path, list[tuple]]:
        session_file = root / f"rollout-{self.thread_id}.jsonl"
        session_file.write_text("{}\n")

        session_map = SessionMap(root / "codex-sessions.json")
        session_map.register(self.thread_id, "%1", "/workspace/project")
        session_map.set_room_id(self.thread_id, "!room:test")
        session_map.set_last_branch(self.thread_id, "main")

        events: list[tuple] = []
        matrix_client = _MatrixClientStub(
            events,
            {"!room:test": "project/main"},
            room_set_name_result=room_set_name_result,
            room_set_name_error=room_set_name_error,
            room_set_name_started=room_set_name_started,
            room_set_name_release=room_set_name_release,
            active_room_set_name_started=active_room_set_name_started,
            active_room_set_name_release=active_room_set_name_release,
        )
        bridge = _RecordingCodexBridge(session_map, matrix_client)

        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.bridge = bridge
        daemon.session_map = session_map
        daemon.watched_sessions = {self.thread_id}
        daemon._pending_assistant = {}
        daemon._last_completed_turn = {}
        daemon._inflight_turns = set()
        daemon._decoration_tasks = set()
        daemon._active_title_tasks = {}
        daemon._title_locks = {}

        return daemon, bridge, session_map, matrix_client, session_file, events

    @staticmethod
    def _completed_messages(text: str, turn_id: str) -> list[dict]:
        return [
            {"role": "assistant", "text": text},
            {"role": "control", "event": "task_complete", "turn_id": turn_id},
        ]

    async def test_branch_refresh_notify_interleaving_does_not_strand_completed_assistant(self):
        with tempfile.TemporaryDirectory() as tmp:
            rename_started = asyncio.Event()
            release_rename = asyncio.Event()
            (
                daemon,
                bridge,
                _session_map,
                _matrix_client,
                session_file,
                _events,
            ) = self._build_watched_daemon(
                Path(tmp),
                room_set_name_started=rename_started,
                room_set_name_release=release_rename,
            )

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                file_task = asyncio.create_task(
                    daemon._on_file_messages(
                        session_file,
                        self._completed_messages("Recovered final response", "turn-1"),
                    )
                )
                try:
                    await asyncio.wait_for(rename_started.wait(), timeout=1)
                    await daemon._on_turn_complete(
                        self.thread_id,
                        "turn-1",
                        "Recovered final response",
                    )
                finally:
                    release_rename.set()
                await file_task

                self.assertEqual(daemon._pending_assistant, {})
                await daemon._on_turn_complete(self.thread_id, "turn-2", "Next final response")

            self.assertEqual(
                bridge.message_calls,
                [
                    (
                        self.thread_id,
                        [{"role": "assistant", "text": "Recovered final response"}],
                        True,
                    ),
                    (
                        self.thread_id,
                        [{"role": "assistant", "text": "Next final response"}],
                        True,
                    ),
                ],
            )

    async def test_rejected_branch_refresh_preserves_metadata_and_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            (
                daemon,
                bridge,
                session_map,
                matrix_client,
                session_file,
                events,
            ) = self._build_watched_daemon(Path(tmp), room_set_name_result=False)

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
                self.assertLogs("codex_matrix.daemon", level="WARNING") as captured,
            ):
                await daemon._on_file_messages(
                    session_file,
                    self._completed_messages("Final response", "turn-1"),
                )

            self.assertEqual(session_map.get(self.thread_id).last_branch, "main")
            self.assertEqual(matrix_client.room_names["!room:test"], "project/main")
            self.assertEqual(
                bridge.message_calls,
                [(self.thread_id, [{"role": "assistant", "text": "Final response"}], True)],
            )
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("room_send"), event_names.index("room_set_name"))
            self.assertTrue(any("branch room title" in message for message in captured.output))

    async def test_raised_branch_refresh_preserves_metadata_and_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            (
                daemon,
                bridge,
                session_map,
                matrix_client,
                session_file,
                events,
            ) = self._build_watched_daemon(
                Path(tmp),
                room_set_name_error=RuntimeError("Matrix rename failed"),
            )

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
                self.assertLogs("codex_matrix.daemon", level="WARNING") as captured,
            ):
                await daemon._on_file_messages(
                    session_file,
                    self._completed_messages("Final response", "turn-1"),
                )

            self.assertEqual(session_map.get(self.thread_id).last_branch, "main")
            self.assertEqual(matrix_client.room_names["!room:test"], "project/main")
            self.assertEqual(
                bridge.message_calls,
                [(self.thread_id, [{"role": "assistant", "text": "Final response"}], True)],
            )
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("room_send"), event_names.index("room_set_name"))
            self.assertTrue(any("branch room title" in message for message in captured.output))

    async def test_acknowledged_branch_refresh_updates_metadata_after_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            (
                daemon,
                bridge,
                session_map,
                matrix_client,
                session_file,
                events,
            ) = self._build_watched_daemon(Path(tmp))

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                await daemon._on_file_messages(
                    session_file,
                    self._completed_messages("Final response", "turn-1"),
                )

            self.assertEqual(session_map.get(self.thread_id).last_branch, "feature-branch")
            self.assertEqual(matrix_client.room_names["!room:test"], "project/feature-branch")
            self.assertEqual(
                bridge.message_calls,
                [(self.thread_id, [{"role": "assistant", "text": "Final response"}], True)],
            )
            event_names = [event[0] for event in events]
            self.assertLess(event_names.index("room_send"), event_names.index("room_set_name"))

    async def test_retirement_waits_for_inflight_branch_refresh_and_ends_room_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            branch_rename_started = asyncio.Event()
            release_branch_rename = asyncio.Event()
            (
                daemon,
                _bridge,
                session_map,
                matrix_client,
                session_file,
                events,
            ) = self._build_watched_daemon(
                Path(tmp),
                active_room_set_name_started=branch_rename_started,
                active_room_set_name_release=release_branch_rename,
            )

            with (
                patch("codex_matrix.daemon.find_session_file", return_value=session_file),
                patch("codex_matrix.bridge.detect_branch", return_value="feature-branch"),
                patch("matrix_bridge.room_name._detect_repo_from_origin", return_value=None),
            ):
                file_task = asyncio.create_task(
                    daemon._on_file_messages(
                        session_file,
                        self._completed_messages("Final response", "turn-1"),
                    )
                )
                await asyncio.wait_for(branch_rename_started.wait(), timeout=1)
                waiting_active_task = daemon._schedule_active_title_restore(self.thread_id)
                await asyncio.sleep(0)
                retire_task = asyncio.create_task(
                    daemon._retire_session(self.thread_id, "test retirement")
                )
                await asyncio.sleep(0)
                retirement_waited = not retire_task.done()
                release_branch_rename.set()
                await asyncio.gather(file_task, retire_task)

            self.assertTrue(retirement_waited)
            self.assertFalse(waiting_active_task.cancelled())
            self.assertFalse(session_map.get(self.thread_id).active)
            self.assertEqual(
                matrix_client.room_names["!room:test"],
                "🔴 project/feature-branch",
            )
            title_events = [event for event in events if event[0] == "room_set_name"]
            self.assertEqual(
                [event[2] for event in title_events],
                ["project/feature-branch", "🔴 project/feature-branch"],
            )


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
            daemon._decoration_tasks = set()
            daemon._active_title_tasks = {}
            daemon._title_locks = {}

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file):
                await daemon._on_turn_complete("thread-1", "turn-1")

            self.assertEqual(daemon.bridge.message_calls, [])
            self.assertEqual(daemon.bridge.ended_calls, ["thread-1"])
            self.assertNotIn("thread-1", daemon.watched_sessions)
