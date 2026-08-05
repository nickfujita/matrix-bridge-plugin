"""Which sessions the Claude bridge is allowed to mirror.

Two independent reasons to decline a hook, both enforced on every handler:

1. The session belongs to another harness. hooks/hooks.json is loaded by Codex
   as well as Claude Code — Codex records it in ~/.codex/config.toml as
   `hooks.state."<plugin>:hooks/hooks.json:<event>:i:j"` — so these handlers
   fire with Codex thread ids and used to open a second, Claude-avatar room for
   a session the Codex bridge was already mirroring.
2. The session is machine-driven and opted out via CCMATRIX_SUPPRESS_SESSION.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from antigravity_matrix import hooks as ag_hooks
from claude_code_matrix import hooks as cc_hooks
from claude_code_matrix.transcript import is_claude_code_payload
from matrix_bridge.config import SUPPRESS_SESSION_ENV, is_suppressed_session
from matrix_bridge.session import SessionMap


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = ROOT / "hooks" / "session-sentinel.sh"

CLAUDE_SESSION_ID = "2b5f63ac-0c10-4399-ae7c-e95667ec21b1"
# UUIDv7, the shape Codex mints for a thread id.
CODEX_THREAD_ID = "019fcf3a-5001-7030-a5bc-fa7929185a57"


def _run(coro):
    return asyncio.run(coro)


class _HookHarness(unittest.TestCase):
    """Runs a real handler with the bridge stubbed out and HOME redirected."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.state_dir = self.home / ".ccmatrix"
        self.state_dir.mkdir(parents=True)
        (self.state_dir / "enabled").touch()
        (self.state_dir / "antigravity-enabled").touch()
        self.projects_dir = self.home / ".claude" / "projects" / "-repo"
        self.projects_dir.mkdir(parents=True)

    def claude_payload(self, event, session_id=CLAUDE_SESSION_ID, **extra):
        """A payload shaped like the ones Claude Code really sends.

        Captured from `claude -p` against an isolated HOME: every event carries
        transcript_path, pointing under <config dir>/projects/.
        """
        payload = {
            "session_id": session_id,
            "transcript_path": str(self.projects_dir / f"{session_id}.jsonl"),
            "cwd": "/repo",
            "hook_event_name": event,
        }
        payload.update(extra)
        return payload

    def codex_payload(self, event, thread_id=CODEX_THREAD_ID, **extra):
        """A payload shaped like the ones Codex sends to the very same hooks.

        Field names taken from the hook payload schema in the Codex binary;
        transcript_path is the rollout file under CODEX_HOME.
        """
        payload = {
            "session_id": thread_id,
            "transcript_path": str(
                self.home / ".codex" / "sessions" / "2026" / "08" / "05"
                / f"rollout-2026-08-05T00-02-13-{thread_id}.jsonl"
            ),
            "cwd": "/repo",
            "hook_event_name": event,
            "source": "startup",
        }
        payload.update(extra)
        return payload

    def call_claude_hook(self, event, payload, env=None):
        """Invoke a Claude hook, recording every outbound side effect."""
        calls = {"bridge": 0, "daemon": 0}

        def _bridge(*args, **kwargs):
            calls["bridge"] += 1
            raise AssertionError("MatrixBridge must not be constructed")

        with (
            patch.object(cc_hooks, "STATE_DIR", self.state_dir),
            patch.object(cc_hooks, "ENABLED_FLAG", self.state_dir / "enabled"),
            patch.object(cc_hooks, "MatrixBridge", _bridge),
            patch.object(cc_hooks, "load_config", lambda: None),
            patch.object(cc_hooks, "_ensure_daemon_running",
                         lambda: calls.__setitem__("daemon", calls["daemon"] + 1)),
            patch.dict(os.environ, env or {}, clear=False),
            patch("pathlib.Path.home", lambda: self.home),
        ):
            if env is None:
                os.environ.pop(SUPPRESS_SESSION_ENV, None)
            result = _run(cc_hooks.HANDLERS[event](payload))

        calls["result"] = result
        return calls

    def session_map(self, name="sessions.json"):
        return SessionMap(self.state_dir / name)


class HarnessOwnershipTests(_HookHarness):
    """A Codex thread must leave no trace in the Claude bridge."""

    def test_codex_payload_is_not_claimed(self):
        with patch("pathlib.Path.home", lambda: self.home):
            self.assertFalse(is_claude_code_payload(self.codex_payload("SessionStart")))

    def test_claude_payload_is_claimed(self):
        with patch("pathlib.Path.home", lambda: self.home):
            self.assertTrue(is_claude_code_payload(self.claude_payload("SessionStart")))

    def test_payload_without_transcript_path_falls_back_to_a_real_transcript(self):
        transcript = self.projects_dir / f"{CLAUDE_SESSION_ID}.jsonl"
        transcript.write_text("")
        with patch("pathlib.Path.home", lambda: self.home):
            self.assertTrue(
                is_claude_code_payload({"session_id": CLAUDE_SESSION_ID})
            )
            self.assertFalse(
                is_claude_code_payload({"session_id": CODEX_THREAD_ID})
            )

    def test_no_hook_registers_or_mirrors_a_codex_thread(self):
        for event in cc_hooks.HANDLERS:
            with self.subTest(event=event):
                # A Codex session owns a tmux pane, so TMUX_PANE is set — this
                # is exactly the state in which the old code registered it.
                self.call_claude_hook(
                    event, self.codex_payload(event), env={"TMUX_PANE": "%0"},
                )
                self.assertIsNone(self.session_map().get(CODEX_THREAD_ID))

    def test_session_start_registers_a_real_claude_session(self):
        # The guard must not become a blanket "do nothing".
        calls = self.call_claude_hook(
            "session_start",
            self.claude_payload("SessionStart"),
            env={"TMUX_PANE": "%3"},
        )
        entry = self.session_map().get(CLAUDE_SESSION_ID)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.tmux_pane, "%3")
        self.assertEqual(calls["daemon"], 1)

    def test_a_codex_entry_left_by_an_older_version_is_retired(self):
        self.session_map().register(CODEX_THREAD_ID, "%0", "/repo")
        self.assertTrue(self.session_map().get(CODEX_THREAD_ID).active)

        self.call_claude_hook("stop", self.codex_payload("Stop"))

        entry = self.session_map().get(CODEX_THREAD_ID)
        self.assertFalse(entry.active, "stale Codex entry must stop routing")

    def test_every_registered_handler_is_guarded(self):
        # Structural, not remembered: a new hook added to HANDLERS without the
        # decorator would reopen both bugs at once.
        for event, handler in cc_hooks.HANDLERS.items():
            with self.subTest(event=event):
                self.assertTrue(
                    getattr(handler, "mirrors_only_owned_sessions", False),
                    f"{event} handler is missing @mirrors_only_owned_sessions",
                )

    def test_every_declared_hook_event_has_a_handler(self):
        # hooks.json is what Codex and Claude Code both read; if it grows an
        # event, the guard has to cover that one too.
        data = json.loads((ROOT / "hooks/hooks.json").read_text())
        declared = set()
        for entries in data["hooks"].values():
            for matcher in entries:
                for hook in matcher["hooks"]:
                    command = hook["command"]
                    if "claude_code_matrix.hooks" in command:
                        declared.add(command.rsplit(" ", 1)[-1])
        self.assertTrue(declared)
        self.assertTrue(declared <= set(cc_hooks.HANDLERS))


class SuppressedSessionTests(_HookHarness):
    """CCMATRIX_SUPPRESS_SESSION: a machine-driven session opts out."""

    def test_truthiness_matches_the_other_boolean_settings(self):
        for value, expected in (
            ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
            ("0", False), ("false", False), ("no", False), ("off", False),
            ("", False), ("   ", False),
        ):
            with self.subTest(value=value):
                with patch.dict(os.environ, {SUPPRESS_SESSION_ENV: value}):
                    self.assertIs(is_suppressed_session(), expected)

        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_suppressed_session())

    def test_no_hook_creates_a_room_or_notifies_when_suppressed(self):
        for event in cc_hooks.HANDLERS:
            with self.subTest(event=event):
                calls = self.call_claude_hook(
                    event,
                    self.claude_payload(event),
                    env={SUPPRESS_SESSION_ENV: "1", "TMUX_PANE": "%3"},
                )
                # MatrixBridge raises if constructed, so reaching here already
                # proves no room, no message, no cc.tts tag was emitted.
                self.assertEqual(calls["bridge"], 0)
                self.assertEqual(calls["daemon"], 0)
                self.assertEqual(calls["result"], {})
                self.assertIsNone(self.session_map().get(CLAUDE_SESSION_ID))

    def test_unset_leaves_behaviour_unchanged(self):
        calls = self.call_claude_hook(
            "session_start",
            self.claude_payload("SessionStart"),
            env={"TMUX_PANE": "%3"},
        )
        self.assertIsNotNone(self.session_map().get(CLAUDE_SESSION_ID))
        self.assertEqual(calls["daemon"], 1)

    def test_an_entry_from_before_the_flag_is_retired(self):
        self.session_map().register(CLAUDE_SESSION_ID, "%3", "/repo")
        self.call_claude_hook(
            "stop",
            self.claude_payload("Stop"),
            env={SUPPRESS_SESSION_ENV: "1"},
        )
        self.assertFalse(self.session_map().get(CLAUDE_SESSION_ID).active)


class AntigravitySuppressionTests(_HookHarness):
    """Same opt-out on the Antigravity package, for symmetry."""

    def _call(self, event, payload, env):
        def _bridge(*args, **kwargs):
            raise AssertionError("AntigravityBridge must not be constructed")

        with (
            patch.object(ag_hooks, "STATE_DIR", self.state_dir),
            patch.object(ag_hooks, "ENABLED_FLAG", self.state_dir / "antigravity-enabled"),
            patch.object(ag_hooks, "AntigravityBridge", _bridge),
            patch.object(ag_hooks, "_ensure_daemon_running", lambda: None),
            patch.dict(os.environ, env, clear=False),
            patch("pathlib.Path.home", lambda: self.home),
        ):
            return _run(ag_hooks.HANDLERS[event](payload))

    def test_no_hook_creates_a_room_when_suppressed(self):
        payload = {"conversationId": "conv-1", "workspacePaths": ["/repo"]}
        for event in ag_hooks.HANDLERS:
            with self.subTest(event=event):
                self.assertEqual(
                    self._call(event, payload, {SUPPRESS_SESSION_ENV: "1"}), {}
                )
                self.assertIsNone(
                    self.session_map("antigravity-sessions.json").get("conv-1")
                )

    def test_every_registered_handler_is_guarded(self):
        for event, handler in ag_hooks.HANDLERS.items():
            with self.subTest(event=event):
                self.assertTrue(
                    getattr(handler, "honours_session_suppression", False),
                    f"{event} handler is missing @honours_session_suppression",
                )


class SentinelSuppressionTests(unittest.TestCase):
    """The sentinel injects phone-bridge rules; a machine session has no phone."""

    def _run_sentinel(self, home, env=None):
        environ = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        environ.update(env or {})
        proc = subprocess.run(
            ["bash", str(SENTINEL)], capture_output=True, text=True, env=environ,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_stays_silent_when_suppressed(self):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / ".ccmatrix"
            state.mkdir()
            (state / "enabled").touch()
            for value in ("1", "true", "yes", "on"):
                with self.subTest(value=value):
                    self.assertEqual(
                        self._run_sentinel(home, {SUPPRESS_SESSION_ENV: value}), {},
                    )

    def test_still_speaks_for_a_human_session(self):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / ".ccmatrix"
            state.mkdir()
            (state / "enabled").touch()
            for value in ("", "0", "false", "off"):
                with self.subTest(value=value):
                    output = self._run_sentinel(home, {SUPPRESS_SESSION_ENV: value})
                    self.assertIn(
                        "go-mobile", output["hookSpecificOutput"]["additionalContext"],
                    )


if __name__ == "__main__":
    unittest.main()
