"""Routing regressions use temporary files and mocks; no live Matrix or tmux."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_matrix.daemon import CodexDaemon


class ChildRolloutRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unmirrored_child_with_parent_session_id_cannot_retire_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Path(tmp) / "rollout-child-thread.jsonl"
            child.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "child-thread",
                    "session_id": "parent-thread",
                    "parent_thread_id": "parent-thread",
                    "thread_source": "subagent",
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": "parent-thread"}}},
                    "cwd": "/tmp/project",
                },
            }) + "\n")
            daemon = CodexDaemon.__new__(CodexDaemon)
            daemon._retire_session = AsyncMock()
            # No session-map lookup, send, or pane mutation is needed to
            # ignore the child. The wrong parent ID is the entire regression.
            await daemon._on_file_messages(child, [{"role": "assistant", "text": "internal work"}])
            daemon._retire_session.assert_awaited_once_with("child-thread", "unmirrored background thread")

    async def test_child_retirement_keeps_existing_parent_active_and_watched(self):
        from matrix_bridge.session import SessionMap
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = SessionMap(root / "sessions.json")
            sessions.register("parent-thread", "%0", "/tmp/project")
            sessions.set_room_id("parent-thread", "!parent:test")
            child = root / "rollout-child-thread.jsonl"
            child.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": "child-thread", "session_id": "parent-thread",
                "thread_source": "subagent", "parent_thread_id": "parent-thread",
            }}) + "\n")
            daemon = CodexDaemon.__new__(CodexDaemon)
            daemon.session_map = sessions
            daemon.bridge = SimpleNamespace(mark_session_ended=AsyncMock())
            daemon.watched_sessions = {"parent-thread"}
            daemon._reset_runtime_state()
            daemon._pending_assistant = {"parent-thread": [{"role": "assistant", "text": "pending reply"}]}
            await daemon._on_file_messages(child, [])
            self.assertTrue(sessions.get("parent-thread").active)
            self.assertEqual(sessions.get("parent-thread").tmux_pane, "%0")
            self.assertIn("parent-thread", daemon.watched_sessions)
            self.assertIn("parent-thread", daemon._pending_assistant)
            daemon.bridge.mark_session_ended.assert_not_awaited()
