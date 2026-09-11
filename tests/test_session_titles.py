"""Offline title tests. Matrix is mocked; native stores use temporary directories."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from matrix_bridge.room_name import build_room_name, STATUS_ENDED
from matrix_bridge.session import SessionMap
from matrix_bridge.session_title import (
    TitleSync, clean_title, native_title, rename_native, resolve_session, tmux_title, claude_hook,
)


class NativeTitles(unittest.TestCase):
    def test_codex_index_latest_record_partial_write_and_home_override(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CODEX_HOME": root}):
            sid = str(uuid4())
            path = Path(root) / "session_index.jsonl"
            path.write_text(json.dumps({"id": sid, "thread_name": "Original"}) + "\n")
            self.assertEqual(native_title("codex", sid), "Original")
            with path.open("a") as out:
                out.write(json.dumps({"id": sid, "thread_name": "New topic"}) + '\n{"id":')
            self.assertEqual(native_title("codex", sid), "New topic")
            self.assertIsNone(native_title("codex", str(uuid4())))

    def test_claude_sdk_rename_preserves_messages_and_reads_generated_title(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": root}), patch("matrix_bridge.session_title.state_dir", return_value=Path(root) / "state"):
            sid = str(uuid4())
            project = Path(root) / "projects" / "-tmp-fixture"
            project.mkdir(parents=True)
            path = project / f"{sid}.jsonl"
            original = json.dumps({"type": "user", "sessionId": sid, "uuid": str(uuid4()),
                                   "message": {"role": "user", "content": "Help with titles"}}) + "\n"
            path.write_text(original + json.dumps({"type": "ai-title", "aiTitle": "Generated topic", "sessionId": sid}) + "\n")
            self.assertEqual(native_title("claude", sid), "Generated topic")
            rename_native("claude", sid, "Shared session titles")
            self.assertEqual(native_title("claude", sid), "Shared session titles")
            self.assertTrue(path.read_text().startswith(original))
            # A live CLI may reappend its cached old title. Keep the pending
            # request visible and apply it via the supported live hook response.
            with path.open("a") as out:
                out.write(json.dumps({"type": "custom-title", "customTitle": "Generated topic", "sessionId": sid}) + "\n")
            self.assertEqual(native_title("claude", sid), "Shared session titles")
            self.assertEqual(claude_hook({"hook_event_name": "UserPromptSubmit", "session_id": sid,
                                          "transcript_path": str(path)}),
                             {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "Shared session titles"}})

    def test_claude_hook_does_not_claim_codex_or_tool_events(self):
        self.assertEqual(claude_hook({"hook_event_name": "UserPromptSubmit", "session_id": str(uuid4()),
                                      "transcript_path": "/tmp/codex/rollout.jsonl"}), {})
        self.assertEqual(claude_hook({"hook_event_name": "PostToolUse"}), {})

    def test_no_directory_based_session_guess_and_child_uses_own_id(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "child-id"}, clear=True):
            self.assertEqual(resolve_session(None, None), ("codex", "child-id"))
        with patch.dict(os.environ, {}, clear=True), patch("matrix_bridge.session_title.session_maps", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Cannot identify"):
                resolve_session(None, None)

    def test_codex_child_cannot_rename_native_title(self):
        with patch("matrix_bridge.session_title.CodexRPC") as rpc:
            rpc.return_value.__enter__.return_value.call.return_value = {"thread": {"source": {"subagent": "parent"}}}
            with self.assertRaisesRegex(ValueError, "primary"):
                rename_native("codex", str(uuid4()), "Oops")
            self.assertEqual(rpc.return_value.__enter__.return_value.call.call_count, 1)

    def test_title_replaces_branch_and_keeps_ended_marker(self):
        with patch("matrix_bridge.room_name.repo_name_from_cwd", return_value="repo"), patch("matrix_bridge.room_name.detect_branch") as git:
            self.assertEqual(build_room_name("/old/main", title="Fix hibernation", branch="main"), "repo · Fix hibernation")
            self.assertEqual(build_room_name("/old/main", title="Fix hibernation", status=STATUS_ENDED), f"{STATUS_ENDED} repo · Fix hibernation")
            git.assert_not_called()

    def test_control_characters_are_removed(self):
        self.assertEqual(clean_title("a\n\tb\x1b\u200bc"), "a b c")
        self.assertEqual(len(clean_title("x" * 200)), 120)


class TitleReconciliation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.smap = SessionMap(Path(self.tmp.name) / "sessions.json")
        self.sid = str(uuid4())
        self.smap.register(self.sid, "%4", "/old/main")
        self.smap.set_room_id(self.sid, "!fixture:example.com")
        self.title = "Fix hibernation"
        self.client = type("Client", (), {"room_set_name": AsyncMock(return_value=True)})()
        for target, replacement in (
            ("session_maps", lambda: [("codex", self.smap)]),
            ("native_title", lambda *_: self.title),
            ("tmux_title", lambda *_: True),
            ("clear_tmux_title", lambda *_: None),
        ):
            patcher = patch("matrix_bridge.session_title." + target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("matrix_bridge.room_name.repo_name_from_cwd", return_value="repo")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sync = TitleSync()

    async def test_debounce_dedupe_manual_change_and_failed_request_retry(self):
        await self.sync.sync(self.client)
        self.client.room_set_name.assert_not_awaited()
        await self.sync.sync(self.client)
        self.client.room_set_name.assert_awaited_once_with("!fixture:example.com", "repo · Fix hibernation")
        await self.sync.sync(self.client)
        self.assertEqual(self.client.room_set_name.await_count, 1)
        self.title = "User renamed this"
        await self.sync.sync(self.client)
        self.assertEqual(self.client.room_set_name.await_count, 1)
        self.client.room_set_name.return_value = False
        await self.sync.sync(self.client)
        self.assertEqual(self.smap.get(self.sid).last_room_name, "repo · Fix hibernation")
        self.client.room_set_name.return_value = True
        await self.sync.sync(self.client)
        self.assertEqual(self.smap.get(self.sid).last_room_name, "repo · User renamed this")

    async def test_ended_and_resumed_states_use_current_title(self):
        await self.sync.sync(self.client, immediate=True)
        self.smap.deregister(self.sid)
        await self.sync.sync(self.client)
        self.client.room_set_name.assert_awaited_with("!fixture:example.com", f"{STATUS_ENDED} repo · Fix hibernation")
        self.smap.register(self.sid, "%4", "/old/main")
        await self.sync.sync(self.client)
        self.client.room_set_name.assert_awaited_with("!fixture:example.com", "repo · Fix hibernation")

    async def test_newest_session_owns_pane_across_harnesses(self):
        other = SessionMap(Path(self.tmp.name) / "other.json")
        other.register("newer", "%4", "/old/main")
        with patch("matrix_bridge.session_title.session_maps", return_value=[("codex", self.smap), ("claude", other)]), patch("matrix_bridge.session_title.tmux_title", return_value=True) as tmux:
            await self.sync.sync(immediate=True)
            tmux.assert_called_once_with("%4", "claude", "newer", self.title, "/old/main")

    async def test_matrix_outage_does_not_block_tmux(self):
        self.client.room_set_name.side_effect = OSError("offline")
        with patch("matrix_bridge.session_title.tmux_title", return_value=True) as tmux:
            with self.assertLogs("matrix_bridge.session_title", level="ERROR"):
                await self.sync.sync(self.client, immediate=True)
            tmux.assert_called_once()
        self.assertIsNone(self.smap.get(self.sid).last_room_name)


@unittest.skipUnless(shutil.which("tmux"), "requires tmux; creates its own isolated server")
class TmuxTitleIntegration(unittest.TestCase):
    def test_idle_name_refresh_literal_text_and_manual_override(self):
        label = "title-test-" + uuid4().hex
        def tm(*args):
            return subprocess.check_output(["tmux", "-L", label, *args], text=True).rstrip("\n")
        def expect_name(expected):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                actual = tm("display-message", "-p", "#{window_name}")
                if actual == expected:
                    return
                time.sleep(0.05)  # tmux schedules automatic renames on its event loop.
            self.assertEqual(actual, expected)
        tm("-f", "/dev/null", "new-session", "-d", "-s", "fixture", "sleep 60")
        try:
            pane = tm("display-message", "-p", "#{pane_id}")
            sock = tm("display-message", "-p", "#{socket_path}")
            tm("set-option", "-w", "automatic-rename-format", "#{@session_title}")
            tm("set-option", "-w", "automatic-rename", "on")
            with patch.dict(os.environ, {"TMUX": sock + ",0,0"}):
                self.assertTrue(tmux_title(pane, "sleep", "fixture", "First topic", "/tmp/worktree"))
                self.assertEqual(tm("display-message", "-p", "#{@session_repo} | #{@session_title}"), "worktree | First topic")
                expect_name("First topic")
                title = "Literal #{window_id} #(echo not-executed)"
                self.assertTrue(tmux_title(pane, "sleep", "fixture", title))
                expect_name(title)
                tm("rename-window", "My manual name")
                self.assertTrue(tmux_title(pane, "sleep", "fixture", "Another topic"))
                self.assertEqual(tm("display-message", "-p", "#{window_name}"), "My manual name")
        finally:
            tm("kill-session", "-t", "fixture")


if __name__ == "__main__":
    unittest.main()
