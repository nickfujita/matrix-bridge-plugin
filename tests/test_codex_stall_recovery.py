"""The overnight-stall defects: a live session went invisible and stayed that way.

A cloud box ran an interactive Codex session whose turn was terminated by a
server-side content filter. Nothing reached the operator's phone for four and a
half hours. Four independent faults had to line up, and each one is pinned here:

1. The session's tmux pane was never recorded, so it kept the file watcher's
   `unknown` placeholder and the staleness reaper — aimed at background
   subagent threads — retired a live interactive session.
2. Retirement was a one-way door, so every later line of a 14-hour rollout was
   discarded.
3. The rollout's own `task_complete` carried the error and the daemon dropped
   it, so even a listening bridge would have mirrored stale progress
   commentary instead of the failure.
4. The notify hook was pinned to the plugin version current when the bridge was
   enabled, and nothing refreshed it on upgrade.

Plus the room-title asymmetries found alongside them: nothing ever renamed a
room back out of the ended state, and the most common way a session ends —
starting the next one in the same pane — never renamed it into that state.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_matrix.cli import _plugin_root_resolution_lines
from codex_matrix.daemon import CodexDaemon
from codex_matrix.transcript import (
    extract_messages_from_offset,
    format_turn_error,
    is_interactive_session_meta,
)
from matrix_bridge.session import SessionMap
from matrix_bridge.tmux import pane_from_process_env, pid_holding_file

INTERACTIVE_ID = "019fd0d2-f39e-7d92-b4b4-a4699fa3b950"
SUBAGENT_ID = "019fd34b-1111-2222-3333-444455556666"

CYBER_POLICY_TEXT = (
    "This content was flagged for possible cybersecurity risk. If this seems "
    "wrong, try rephrasing your request."
)


def _rollout(tmp: Path, thread_id: str, *, subagent: bool = False) -> Path:
    """Write a rollout whose session_meta matches what Codex really writes."""
    payload = {
        "session_id": thread_id,
        "id": thread_id,
        "cwd": "/repo",
        "originator": "codex-tui",
        "source": {"subagent": {"thread_spawn": {}}} if subagent else "cli",
        "thread_source": "subagent" if subagent else "user",
    }
    if subagent:
        payload["parent_thread_id"] = INTERACTIVE_ID

    path = tmp / f"rollout-2026-08-05T07-28-33-{thread_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n"
        + json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "carry on"}],
            },
        }) + "\n"
    )
    return path


class _BridgeStub:
    """Stands in for CodexBridge, mirroring the parts of its contract we rely on."""

    def __init__(self, session_map: SessionMap | None = None, room_id: str = "!room:test"):
        self.session_map = session_map
        self.room_id = room_id
        self.message_calls: list[tuple[str, list[dict], bool]] = []
        self.created: list[str] = []
        self.ended: list[str] = []
        self.activated: list[str] = []
        self.create_delay = 0.0

    async def create_room(self, session_id: str, cwd: str) -> str:
        await asyncio.sleep(self.create_delay)
        self.created.append(session_id)
        if self.session_map:
            self.session_map.set_room_id(session_id, self.room_id)
        return self.room_id

    async def send_messages(self, session_id: str, messages: list[dict], notify_final: bool = False) -> int:
        self.message_calls.append((session_id, list(messages), notify_final))
        return len(messages)

    async def set_typing(self, session_id: str, typing: bool) -> None:
        pass

    # The real bridge records what the room title now shows; the reconciler
    # depends on that write happening, so the stub does it too.
    async def mark_session_ended(self, session_id: str) -> bool:
        self.ended.append(session_id)
        if self.session_map:
            self.session_map.set_room_marked_ended(session_id, True)
        return True

    async def mark_session_active(self, session_id: str) -> bool:
        self.activated.append(session_id)
        if self.session_map:
            self.session_map.set_room_marked_ended(session_id, False)
        return True

    async def refresh_branch_if_changed(self, session_id: str) -> bool:
        return True


class _WatcherStub:
    def __init__(self):
        self.watched: list[Path] = []

    def watch_file(self, path: Path) -> None:
        self.watched.append(path)

    def watch_file_from_start(self, path: Path) -> None:
        self.watched.append(path)


def _daemon(tmp: Path) -> CodexDaemon:
    daemon = CodexDaemon.__new__(CodexDaemon)
    daemon.session_map = SessionMap(tmp / "codex-sessions.json")
    daemon.bridge = _BridgeStub(daemon.session_map)
    daemon.watcher = _WatcherStub()
    daemon.watched_sessions = set()
    daemon.running = True
    daemon._reset_runtime_state()
    return daemon


async def _drain(daemon: CodexDaemon) -> None:
    """Await the background title-decoration tasks the daemon schedules."""
    tasks = list(daemon._decoration_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


class ReaperDiscriminatorTests(unittest.IsolatedAsyncioTestCase):
    """A missing pane means "background thread" only for background threads."""

    async def test_interactive_session_without_a_pane_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, INTERACTIVE_ID)
            # Far past the staleness cutoff — a long tool call looks exactly
            # like this, and it is what retired the real session.
            old = time.time() - 3600
            os.utime(session_file, (old, old))
            daemon.session_map.register(INTERACTIVE_ID, "unknown", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file), \
                 patch("codex_matrix.daemon.pane_for_open_file", return_value=None):
                await daemon._cleanup_ended_sessions()

            entry = daemon.session_map.get(INTERACTIVE_ID)
            self.assertTrue(entry.active, "an interactive session is not a stale subagent")
            self.assertEqual(daemon.bridge.ended, [])

    async def test_subagent_session_without_a_pane_is_still_reaped(self):
        # The reaper still has a job: these really do end silently, and on a
        # working machine they are the majority of tracked sessions.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, SUBAGENT_ID, subagent=True)
            old = time.time() - 3600
            os.utime(session_file, (old, old))
            daemon.session_map.register(SUBAGENT_ID, "unknown", "/repo")
            daemon.session_map.set_room_id(SUBAGENT_ID, "!room:test")

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file), \
                 patch("codex_matrix.daemon.pane_for_open_file", return_value=None):
                await daemon._cleanup_ended_sessions()

            self.assertFalse(daemon.session_map.get(SUBAGENT_ID).active)
            self.assertEqual(daemon.bridge.ended, [SUBAGENT_ID])

    async def test_metadata_classifier_separates_the_two(self):
        interactive = {"originator": "codex-tui", "source": "cli", "thread_source": "user"}
        self.assertTrue(is_interactive_session_meta(interactive))

        for meta in (
            {"originator": "codex-tui", "thread_source": "subagent"},
            {"originator": "codex-tui", "source": {"subagent": {}}},
            {"originator": "codex-tui", "parent_thread_id": INTERACTIVE_ID},
            {"originator": "codex_exec", "source": "exec"},
            None,
            {},
        ):
            with self.subTest(meta=meta):
                self.assertFalse(is_interactive_session_meta(meta))


class PaneBackfillTests(unittest.IsolatedAsyncioTestCase):
    """The daemon establishes the pane itself rather than waiting for the hook."""

    async def test_provisional_entry_gets_its_pane_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, INTERACTIVE_ID)
            old = time.time() - 3600
            os.utime(session_file, (old, old))
            daemon.session_map.register(INTERACTIVE_ID, "unknown", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file), \
                 patch("codex_matrix.daemon.pane_for_open_file", return_value="%0"), \
                 patch("codex_matrix.daemon.pane_current_command", return_value="codex"):
                await daemon._cleanup_ended_sessions()

            entry = daemon.session_map.get(INTERACTIVE_ID)
            self.assertEqual(entry.tmux_pane, "%0")
            self.assertTrue(entry.active)

    def test_pane_is_read_from_the_owning_process_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            (proc / "1234").mkdir(parents=True)
            (proc / "1234" / "environ").write_bytes(
                b"PATH=/usr/bin\x00TMUX_PANE=%3\x00TERM=xterm\x00"
            )
            self.assertEqual(pane_from_process_env(1234, proc_root=proc), "%3")

    def test_a_process_launched_outside_tmux_reports_no_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp) / "proc"
            (proc / "99").mkdir(parents=True)
            (proc / "99" / "environ").write_bytes(b"PATH=/usr/bin\x00")
            self.assertIsNone(pane_from_process_env(99, proc_root=proc))
            self.assertIsNone(pane_from_process_env(4242, proc_root=proc))

    def test_the_rollout_holder_is_found_through_its_open_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "rollout.jsonl"
            target.write_text("{}\n")
            other = root / "unrelated.log"
            other.write_text("")

            proc = root / "proc"
            (proc / "10" / "fd").mkdir(parents=True)
            (proc / "10" / "fd" / "3").symlink_to(other)
            (proc / "20" / "fd").mkdir(parents=True)
            (proc / "20" / "fd" / "7").symlink_to(target)

            self.assertEqual(pid_holding_file(target, proc_root=proc), 20)


class NotifyHookPaneTests(unittest.IsolatedAsyncioTestCase):
    """The hook records the pane even when its own environment lacks one."""

    async def test_a_provisional_entry_is_backfilled_by_the_notify_hook(self):
        from codex_matrix import notify_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            (state / "codex-enabled").touch()
            session_file = _rollout(root, INTERACTIVE_ID)

            session_map = SessionMap(state / "codex-sessions.json")
            # Exactly what the file watcher leaves behind.
            session_map.register(INTERACTIVE_ID, "unknown", "/repo")

            payload = json.dumps({
                "type": "agent-turn-complete",
                "thread-id": INTERACTIVE_ID,
                "turn-id": "t1",
                "cwd": "/repo",
                "last-assistant-message": "done",
            })

            with patch.object(notify_handler, "STATE_DIR", state), \
                 patch.object(notify_handler, "ENABLED_FLAG", state / "codex-enabled"), \
                 patch.object(notify_handler, "find_session_file", return_value=session_file), \
                 patch.object(notify_handler, "pane_for_open_file", return_value="%0"), \
                 patch.object(notify_handler, "_ensure_daemon_running", lambda: None), \
                 patch.dict(os.environ, {"TMUX_PANE": ""}, clear=False), \
                 patch.object(notify_handler.sys, "argv", ["notify", payload]):
                notify_handler.handle_notify()

            entry = SessionMap(state / "codex-sessions.json").get(INTERACTIVE_ID)
            self.assertEqual(
                entry.tmux_pane, "%0",
                "an entry left provisional is what feeds a live session to the reaper",
            )
            self.assertTrue(entry.active)


class ReversibleRetirementTests(unittest.IsolatedAsyncioTestCase):
    """One false positive must not cost the rest of the session."""

    async def test_new_activity_un_retires_and_keeps_the_same_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, INTERACTIVE_ID)

            daemon.session_map.register(INTERACTIVE_ID, "unknown", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!original:test")
            daemon.session_map.deregister(INTERACTIVE_ID)
            daemon.session_map.set_room_marked_ended(INTERACTIVE_ID, True)

            await daemon._on_file_messages(
                session_file, [{"role": "assistant", "text": "still here"}],
            )
            await _drain(daemon)

            entry = daemon.session_map.get(INTERACTIVE_ID)
            self.assertTrue(entry.active, "fresh rollout activity means the session is alive")
            self.assertEqual(entry.room_id, "!original:test", "must reuse the operator's room")
            self.assertEqual(daemon.bridge.created, [], "no second room may appear")
            self.assertIn(INTERACTIVE_ID, daemon.watched_sessions)
            # And the room stops claiming the session is over.
            self.assertEqual(daemon.bridge.activated, [INTERACTIVE_ID])
            self.assertFalse(daemon.session_map.get(INTERACTIVE_ID).room_marked_ended)

    async def test_a_retired_subagent_thread_is_not_resurrected(self):
        # Un-retirement is gated on the session still qualifying for mirroring,
        # so the suppression rules keep working.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, SUBAGENT_ID, subagent=True)
            daemon.session_map.register(SUBAGENT_ID, "unknown", "/repo")
            daemon.session_map.set_room_id(SUBAGENT_ID, "!room:test")
            daemon.session_map.deregister(SUBAGENT_ID)

            await daemon._on_file_messages(
                session_file, [{"role": "assistant", "text": "background result"}],
            )

            self.assertFalse(daemon.session_map.get(SUBAGENT_ID).active)
            self.assertEqual(daemon.bridge.message_calls, [])


class TurnErrorMirroringTests(unittest.IsolatedAsyncioTestCase):
    """The change that turns a silent night into a phone buzz."""

    def test_the_rollout_control_event_carries_the_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text(json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "f51c3024",
                    "last_agent_message": None,
                    "error": {
                        "message": CYBER_POLICY_TEXT,
                        "codex_error_info": "cyber_policy",
                    },
                },
            }) + "\n")

            messages, _ = extract_messages_from_offset(path, 0)
            self.assertEqual(len(messages), 1)
            event = messages[0]
            self.assertEqual(event["event"], "task_complete")
            self.assertEqual(event["error_kind"], "cyber_policy")
            self.assertIn("flagged", event["error"])
            self.assertIsNone(event["last_agent_message"])

    def test_a_clean_completion_formats_to_nothing(self):
        self.assertIsNone(format_turn_error({
            "event": "task_complete", "turn_id": "t1",
            "error": "", "error_kind": "", "last_agent_message": "All done.",
        }))

    def test_an_errored_completion_names_the_kind_and_the_absence_of_a_reply(self):
        text = format_turn_error({
            "error": CYBER_POLICY_TEXT,
            "error_kind": "cyber_policy",
            "last_agent_message": None,
        })
        self.assertIn("cyber_policy", text)
        self.assertIn("flagged", text)
        self.assertIn("(no final message)", text)

    async def test_the_error_is_mirrored_instead_of_the_stale_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, INTERACTIVE_ID)
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")

            # Exactly the shape of the incident: mid-turn progress commentary
            # buffered, then the turn dies without ever producing a reply.
            stale = "The exact-commit re-reviews are running now…"
            await daemon._on_file_messages(session_file, [
                {"role": "assistant", "text": stale},
                {
                    "role": "control",
                    "event": "task_complete",
                    "turn_id": "f51c3024",
                    "error": CYBER_POLICY_TEXT,
                    "error_kind": "cyber_policy",
                    "last_agent_message": None,
                },
            ])
            await _drain(daemon)

            self.assertEqual(len(daemon.bridge.message_calls), 1)
            _session, messages, notify_final = daemon.bridge.message_calls[0]
            self.assertTrue(notify_final, "an errored turn must reach the phone")
            self.assertEqual(len(messages), 1)
            body = messages[0]["text"]
            self.assertIn("cyber_policy", body)
            self.assertNotIn(
                "re-reviews", body,
                "mid-turn commentary must not be presented as the turn's result",
            )
            self.assertEqual(daemon._pending_assistant.get(INTERACTIVE_ID, []), [])

    async def test_a_clean_turn_still_flushes_its_buffered_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, INTERACTIVE_ID)
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")

            await daemon._on_file_messages(session_file, [
                {"role": "assistant", "text": "Here is the answer."},
                {"role": "control", "event": "task_complete", "turn_id": "t1",
                 "error": "", "error_kind": "", "last_agent_message": "Here is the answer."},
            ])
            await _drain(daemon)

            _session, messages, notify_final = daemon.bridge.message_calls[0]
            self.assertTrue(notify_final)
            self.assertEqual(messages, [{"role": "assistant", "text": "Here is the answer."}])


class RoomTitleReconcilerTests(unittest.IsolatedAsyncioTestCase):
    """`active` and what the room title says must not drift apart."""

    async def test_a_resumed_session_loses_the_ended_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp))
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")
            # Retired and renamed, then resumed — the state nothing repaired.
            daemon.session_map.set_room_marked_ended(INTERACTIVE_ID, True)

            await daemon._reconcile_room_status()

            self.assertEqual(daemon.bridge.activated, [INTERACTIVE_ID])
            self.assertFalse(daemon.session_map.get(INTERACTIVE_ID).room_marked_ended)

    async def test_same_pane_succession_marks_the_predecessors_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp))
            old_id, new_id = "old-session-id", "new-session-id"

            daemon.session_map.register(old_id, "%1", "/repo")
            daemon.session_map.set_room_id(old_id, "!old:test")
            daemon.session_map.set_room_marked_ended(old_id, False)

            # The normal end of an interactive session: quit, start the next
            # one in the same pane. This retires the predecessor purely in the
            # data layer, with no Matrix call anywhere.
            daemon.session_map.register(new_id, "%1", "/repo")
            daemon.session_map.set_room_id(new_id, "!new:test")

            self.assertFalse(daemon.session_map.get(old_id).active)
            self.assertFalse(
                daemon.session_map.get(old_id).room_marked_ended,
                "precondition: nothing renamed it, which is the bug",
            )

            await daemon._reconcile_room_status()

            self.assertEqual(daemon.bridge.ended, [old_id])
            self.assertTrue(daemon.session_map.get(old_id).room_marked_ended)
            self.assertNotIn(new_id, daemon.bridge.ended)

    async def test_agreeing_titles_are_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp))
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")

            await daemon._reconcile_room_status()
            await daemon._reconcile_room_status()

            self.assertEqual(daemon.bridge.activated, [])
            self.assertEqual(daemon.bridge.ended, [])

    async def test_a_session_without_a_room_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp))
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.deregister(INTERACTIVE_ID)

            await daemon._reconcile_room_status()

            self.assertEqual(daemon.bridge.ended, [])


class RoomCreationRaceTests(unittest.IsolatedAsyncioTestCase):
    """The map entry is the source of truth; a retry must reuse, not duplicate."""

    async def test_concurrent_callers_create_one_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp))
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            # Room creation is a Matrix round-trip; both callers are in flight
            # across it, which is how the duplicates were produced.
            daemon.bridge.create_delay = 0.05

            await asyncio.gather(
                daemon._ensure_room(INTERACTIVE_ID, "/repo"),
                daemon._ensure_room(INTERACTIVE_ID, "/repo"),
            )

            self.assertEqual(
                daemon.bridge.created, [INTERACTIVE_ID],
                "the second caller must observe the first caller's room",
            )

    async def test_a_later_call_reuses_the_existing_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = _daemon(Path(tmp))
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!existing:test")

            await daemon._ensure_room(INTERACTIVE_ID, "/repo")

            self.assertEqual(daemon.bridge.created, [])
            self.assertEqual(
                daemon.session_map.get(INTERACTIVE_ID).room_id, "!existing:test",
            )


class StallNoticeTests(unittest.IsolatedAsyncioTestCase):
    """"Did the agent stop?" is not answerable from turn-complete events."""

    def _stalled(self, root: Path):
        daemon = _daemon(root)
        session_file = _rollout(root, INTERACTIVE_ID)
        old = time.time() - (100 * 60)
        os.utime(session_file, (old, old))
        daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
        daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")
        return daemon, session_file

    async def test_a_running_turn_that_stops_progressing_posts_one_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon, session_file = self._stalled(Path(tmp))
            daemon._active_turns.add(INTERACTIVE_ID)

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file), \
                 patch("codex_matrix.daemon.pane_current_command", return_value="codex"):
                await daemon._cleanup_ended_sessions()
                await daemon._cleanup_ended_sessions()

            self.assertEqual(len(daemon.bridge.message_calls), 1, "exactly one, not one per pass")
            _session, messages, notify_final = daemon.bridge.message_calls[0]
            self.assertFalse(notify_final, "a state line is an m.notice, not a push")
            self.assertEqual(messages[0]["role"], "tool")
            self.assertIn("No activity", messages[0]["text"])

    async def test_an_idle_session_with_no_running_turn_stays_quiet(self):
        # Sitting at the prompt overnight is not a stall.
        with tempfile.TemporaryDirectory() as tmp:
            daemon, session_file = self._stalled(Path(tmp))

            with patch("codex_matrix.daemon.find_session_file", return_value=session_file), \
                 patch("codex_matrix.daemon.pane_current_command", return_value="codex"):
                await daemon._cleanup_ended_sessions()

            self.assertEqual(daemon.bridge.message_calls, [])

    async def test_turn_boundaries_arm_and_disarm_the_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = _daemon(root)
            session_file = _rollout(root, INTERACTIVE_ID)
            daemon.session_map.register(INTERACTIVE_ID, "%0", "/repo")
            daemon.session_map.set_room_id(INTERACTIVE_ID, "!room:test")

            await daemon._on_file_messages(
                session_file, [{"role": "control", "event": "task_started", "turn_id": "t1"}],
            )
            self.assertIn(INTERACTIVE_ID, daemon._active_turns)

            # ESC closes a turn too; without this the daemon would believe a
            # turn was live forever and keep reporting stalls.
            await daemon._on_file_messages(
                session_file, [{"role": "control", "event": "turn_aborted", "turn_id": "t1"}],
            )
            self.assertNotIn(INTERACTIVE_ID, daemon._active_turns)


class NotifyScriptVersionResolutionTests(unittest.TestCase):
    """The hook must follow the plugin across upgrades, not pin to install time."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self._home_patch = patch(
            "codex_matrix.cli.Path.home", return_value=Path(self._home.name) / "home"
        )
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def _cache(self, root: Path, *versions: str) -> Path:
        cache = root / "cache" / "claude-code-matrix"
        for version in versions:
            marker = (
                cache / version / "packages" / "codex-matrix" / "src" / "codex_matrix"
            )
            marker.mkdir(parents=True)
            (marker / "notify_handler.py").write_text("")
            manifest = cache / version / ".claude-plugin"
            manifest.mkdir()
            (manifest / "plugin.json").write_text(
                json.dumps({"name": "claude-code-matrix", "version": version})
            )
        return cache

    @staticmethod
    def _install_revision(candidate: Path, revision: str) -> None:
        (candidate / ".codex-marketplace-install.json").write_text(
            json.dumps({"revision": revision})
        )

    @staticmethod
    def _active_revision(home: Path, revision: str) -> None:
        config_dir = home / ".codex"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(
            "[marketplaces.claude-code-matrix]\n"
            f'last_revision = "{revision}"\n'
        )

    def _resolve(self, project_root: Path) -> str:
        script = "\n".join(_plugin_root_resolution_lines(project_root))
        result = subprocess.run(
            ["bash", "-c", script + '\necho "$matrix_root"'],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_it_follows_a_version_bump_made_after_the_script_was_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The script is written while 0.5.7 is current...
            cache = self._cache(Path(tmp), "0.5.7")
            pinned = cache / "0.5.7"
            self.assertEqual(self._resolve(pinned), str(pinned))

            # ...then an upgrade lands 0.5.10 beside it and nothing rewrites
            # the script. This is the exact skew observed in the field.
            self._cache(Path(tmp), "0.5.10")
            self.assertEqual(
                self._resolve(pinned), str(cache / "0.5.10"),
                "the hook must run the version the daemon is running",
            )

    def test_version_ordering_is_numeric_not_lexical(self):
        # The whole point: "0.5.10" sorts *below* "0.5.7" as text, so a plain
        # glob would have picked the older one and looked like it worked.
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), "0.5.7", "0.5.9", "0.5.10", "0.5.11")
            self.assertEqual(self._resolve(cache / "0.5.7"), str(cache / "0.5.11"))

    def test_optional_v_prefix_and_whitespace_paths_use_numeric_version_order(self):
        """The generated shell must not split cache paths or order v-prefixed versions textually."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cache = self._cache(root / "cache with spaces", "v0.5.2", "0.5.12")
            with patch("codex_matrix.cli.Path.home", return_value=home):
                self.assertEqual(self._resolve(cache / "v0.5.2"), str(cache / "0.5.12"))

    def test_a_directory_without_the_handler_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), "0.5.7")
            (cache / "0.5.99").mkdir(parents=True)  # partial/aborted install
            self.assertEqual(self._resolve(cache / "0.5.7"), str(cache / "0.5.7"))

    def test_a_plain_checkout_uses_itself_when_no_installed_cache_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            checkout = Path(tmp) / "matrix-bridge-plugin"
            checkout.mkdir()
            with patch("codex_matrix.cli.Path.home", return_value=home):
                self.assertEqual(self._resolve(checkout), str(checkout))

    def test_a_plain_checkout_prefers_the_active_codex_plugin_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            checkout = root / "matrix-bridge-plugin"
            checkout.mkdir()
            active = (
                home
                / ".codex/plugins/cache/claude-code-matrix/claude-code-matrix/0.5.11"
            )
            marker = active / "packages/codex-matrix/src/codex_matrix/notify_handler.py"
            marker.parent.mkdir(parents=True)
            marker.write_text("")
            manifest = active / ".claude-plugin"
            manifest.mkdir()
            (manifest / "plugin.json").write_text(
                json.dumps({"name": "claude-code-matrix", "version": "0.5.11"})
            )

            with patch("codex_matrix.cli.Path.home", return_value=home):
                self.assertEqual(self._resolve(checkout), str(active))

    def test_a_later_directory_with_a_mismatched_manifest_is_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(Path(tmp), "0.5.11", "0.5.12")
            (cache / "0.5.12/.claude-plugin/plugin.json").write_text(
                json.dumps({"name": "claude-code-matrix", "version": "0.5.11"})
            )

            self.assertEqual(self._resolve(cache / "0.5.11"), str(cache / "0.5.11"))

    def test_active_marketplace_revision_beats_a_later_cached_version(self):
        """A rollback selected by Codex must beat generic highest-version selection."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cache = self._cache(root, "v0.5.2", "0.5.12")
            self._install_revision(cache / "v0.5.2", "rollback-revision")
            self._install_revision(cache / "0.5.12", "newer-revision")
            self._active_revision(home, "rollback-revision")

            with patch("codex_matrix.cli.Path.home", return_value=home):
                self.assertEqual(self._resolve(cache / "0.5.12"), str(cache / "v0.5.2"))

    def test_unmatched_active_revision_falls_back_to_the_enable_time_root(self):
        """Explicit but unmatched active metadata must not run an inactive cache release."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            cache = self._cache(root, "v0.5.2", "0.5.12")
            self._install_revision(cache / "v0.5.2", "older")
            self._install_revision(cache / "0.5.12", "newer")
            self._active_revision(home, "not-installed")

            with patch("codex_matrix.cli.Path.home", return_value=home):
                self.assertEqual(self._resolve(cache / "v0.5.2"), str(cache / "v0.5.2"))

    def test_symlinked_root_and_marker_are_not_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self._cache(root, "0.5.11")
            target = self._cache(root / "targets", "0.5.99") / "0.5.99"
            (cache / "0.5.99").symlink_to(target, target_is_directory=True)
            marker_target = root / "marker-target.py"
            marker_target.write_text("")
            bad_marker = self._cache(root, "0.5.12") / "0.5.12"
            marker = bad_marker / "packages/codex-matrix/src/codex_matrix/notify_handler.py"
            marker.unlink()
            marker.symlink_to(marker_target)

            self.assertEqual(self._resolve(cache / "0.5.11"), str(cache / "0.5.11"))

    def test_candidate_with_a_symlinked_intermediate_component_is_not_selected(self):
        """Marker containment must reject a package tree redirected outside the cache."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = self._cache(root, "0.5.11", "0.5.12")
            candidate = cache / "0.5.12"
            packages = candidate / "packages"
            external = root / "external-packages"
            external_marker = external / "codex-matrix/src/codex_matrix"
            external_marker.mkdir(parents=True)
            (external_marker / "notify_handler.py").write_text("")
            for child in packages.iterdir():
                if child.is_dir():
                    for nested in sorted(child.rglob("*"), reverse=True):
                        if nested.is_file() or nested.is_symlink():
                            nested.unlink()
                        elif nested.is_dir():
                            nested.rmdir()
                    child.rmdir()
            packages.rmdir()
            packages.symlink_to(external, target_is_directory=True)

            self.assertEqual(self._resolve(cache / "0.5.11"), str(cache / "0.5.11"))


if __name__ == "__main__":
    unittest.main()
