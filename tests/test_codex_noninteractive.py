"""`codex exec` runs stay off the phone; interactive sessions do not.

An exec run is started by a script, runs one turn and exits. Nobody is at a
terminal, and the bridge's inbound path types phone replies into a tmux pane, so
its room could never be answered even if someone tried — it is write-only noise.
A single automation round opens several.

The rule has to hold on the daemon's watchdog path, which is where most sessions
are discovered and which never sees the environment of whatever spawned the CLI.
That is why the signal is rollout metadata and the opt-in is a prompt marker.

Field values checked against 514 real rollouts on a working machine:

    originator     source  interactive?
    codex_exec     exec    no    (201)
    codex_exec     dict    no    (49, exec-spawned subagents)
    codex-tui      cli     yes   (42)
    codex_cli_rs   cli     yes   (11, older builds)
    codex-tui      dict    no    (211, subagent threads — pre-existing rule)
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_matrix.daemon import CodexDaemon
from codex_matrix.transcript import (
    extract_session_meta,
    has_force_mirror_marker,
    is_noninteractive_session_meta,
    is_unmirrored_session,
    is_unmirrored_session_meta,
)


THREAD_ID = "019fcf3a-5001-7030-a5bc-fa7929185a57"
FORCE_MARKER = "CCMATRIX_FORCE_MIRROR"


def _rollout(
    tmp: Path,
    *,
    originator: str,
    source,
    thread_source: str | None = "user",
    prompt: str = "Summarise the alerts",
) -> Path:
    """Write a rollout whose session_meta matches what Codex really writes."""
    path = tmp / f"rollout-2026-08-05T00-02-13-{THREAD_ID}.jsonl"
    meta = {
        "type": "session_meta",
        "payload": {
            "session_id": THREAD_ID,
            "id": THREAD_ID,
            "cwd": "/repo",
            "originator": originator,
            "cli_version": "0.146.0",
            "source": source,
            "thread_source": thread_source,
        },
    }
    user = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        },
    }
    path.write_text(json.dumps(meta) + "\n" + json.dumps(user) + "\n")
    return path


class NonInteractiveMetadataTests(unittest.TestCase):
    def test_exec_runs_are_non_interactive(self):
        for originator, source in (
            ("codex_exec", "exec"),
            ("codex_exec", {"subagent": "review"}),   # exec-spawned subagent
            ("something-else", "exec"),               # source alone is enough
            ("codex_exec", None),                     # originator alone is enough
        ):
            with self.subTest(originator=originator, source=source):
                self.assertTrue(
                    is_noninteractive_session_meta(
                        {"originator": originator, "source": source}
                    )
                )

    def test_interactive_sessions_are_not(self):
        for originator, source in (
            ("codex-tui", "cli"),
            ("codex_cli_rs", "cli"),
        ):
            with self.subTest(originator=originator, source=source):
                meta = {"originator": originator, "source": source, "thread_source": "user"}
                self.assertFalse(is_noninteractive_session_meta(meta))
                self.assertFalse(is_unmirrored_session_meta(meta))

    def test_a_dict_valued_source_does_not_crash_the_string_check(self):
        # `source` is a dict for every subagent thread — 260 of 514 rollouts on
        # the machine this was measured on.
        meta = {"originator": "codex-tui", "source": {"subagent": {"thread_spawn": {"depth": 1}}}}
        self.assertFalse(is_noninteractive_session_meta(meta))
        self.assertTrue(is_unmirrored_session_meta(meta))   # still a subagent

    def test_empty_metadata_is_not_suppressed(self):
        self.assertFalse(is_noninteractive_session_meta(None))
        self.assertFalse(is_noninteractive_session_meta({}))

    def test_originator_extraction_survives_the_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _rollout(Path(tmp), originator="codex_exec", source="exec")
            self.assertEqual(extract_session_meta(path)["originator"], "codex_exec")
            self.assertTrue(is_unmirrored_session(path))


class ForceMirrorMarkerTests(unittest.TestCase):
    def test_the_marker_overrides_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _rollout(
                Path(tmp), originator="codex_exec", source="exec",
                prompt=f"{FORCE_MARKER}\nSummarise today's alerts",
            )
            self.assertTrue(has_force_mirror_marker(path))
            self.assertTrue(
                is_unmirrored_session_meta(extract_session_meta(path)),
                "metadata still says exec",
            )
            self.assertFalse(
                is_unmirrored_session(path),
                "an explicit opt-in must win over the exec rule",
            )

    def test_it_also_frees_a_subagent_thread(self):
        # The marker is an explicit human instruction; it would be surprising
        # for it to work on one suppression rule and silently not the other.
        with tempfile.TemporaryDirectory() as tmp:
            path = _rollout(
                Path(tmp), originator="codex-tui", source={"subagent": {"thread_spawn": {}}},
                thread_source="subagent", prompt=f"{FORCE_MARKER} check the build",
            )
            self.assertFalse(is_unmirrored_session(path))

    def test_an_ordinary_prompt_carries_no_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _rollout(Path(tmp), originator="codex-tui", source="cli")
            self.assertFalse(has_force_mirror_marker(path))

    def test_a_missing_file_is_not_an_opt_in(self):
        self.assertFalse(has_force_mirror_marker(Path("/nonexistent/rollout.jsonl")))


class _BridgeStub:
    def __init__(self):
        self.rooms_created: list[str] = []
        self.ended: list[str] = []

    async def create_room(self, thread_id: str, cwd: str) -> str:
        self.rooms_created.append(thread_id)
        return "!room:test"

    async def send_messages(self, thread_id: str, messages: list[dict], notify_final: bool = False) -> int:
        return len(messages)

    async def set_typing(self, thread_id: str, typing: bool) -> None:
        pass

    async def mark_session_ended(self, thread_id: str) -> None:
        self.ended.append(thread_id)


class _WatcherStub:
    def __init__(self):
        self.watched: list[Path] = []

    def watch_file(self, path: Path) -> None:
        self.watched.append(path)

    def watch_file_from_start(self, path: Path) -> None:
        self.watched.append(path)


class WatchdogPathTests(unittest.IsolatedAsyncioTestCase):
    """The path that matters: discovery by file watcher, no environment in sight."""

    def _daemon(self, tmp: Path):
        from matrix_bridge.session import SessionMap

        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.bridge = _BridgeStub()
        daemon.session_map = SessionMap(tmp / "codex-sessions.json")
        daemon.watcher = _WatcherStub()
        daemon.watched_sessions = set()
        daemon._reset_runtime_state()
        return daemon

    async def _feed(self, daemon, path: Path):
        await daemon._on_file_messages(path, [{"role": "assistant", "text": "done"}])

    async def test_an_exec_run_gets_no_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            daemon = self._daemon(tmp)
            path = _rollout(tmp, originator="codex_exec", source="exec")

            await self._feed(daemon, path)

            self.assertEqual(daemon.bridge.rooms_created, [])
            self.assertIsNone(daemon.session_map.get(THREAD_ID))

    async def test_an_interactive_session_still_gets_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            daemon = self._daemon(tmp)
            path = _rollout(tmp, originator="codex-tui", source="cli")

            await self._feed(daemon, path)

            self.assertEqual(daemon.bridge.rooms_created, [THREAD_ID])
            self.assertTrue(daemon.session_map.get(THREAD_ID).active)

    async def test_the_opt_in_gets_an_exec_run_its_room_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            daemon = self._daemon(tmp)
            path = _rollout(
                tmp, originator="codex_exec", source="exec",
                prompt=f"{FORCE_MARKER}\nSummarise today's alerts",
            )

            await self._feed(daemon, path)

            self.assertEqual(daemon.bridge.rooms_created, [THREAD_ID])

    async def test_an_exec_session_registered_by_an_older_version_is_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            daemon = self._daemon(tmp)
            path = _rollout(tmp, originator="codex_exec", source="exec")
            daemon.session_map.register(THREAD_ID, "%0", "/repo")

            await self._feed(daemon, path)

            entry = daemon.session_map.get(THREAD_ID)
            self.assertFalse(entry.active, "a stale exec room must stop routing")


class NotifyPathTests(unittest.TestCase):
    """The notify hook fires at turn-complete and must agree with the watcher."""

    def _run_notify(self, tmp: Path, path: Path, payload_extra: dict):
        import os
        import sys
        from codex_matrix import notify_handler

        state_dir = tmp / "state"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "codex-enabled").touch()
        payload = {"type": "agent-turn-complete", "thread-id": THREAD_ID, "cwd": "/repo"}
        payload.update(payload_extra)

        with (
            patch.object(notify_handler, "STATE_DIR", state_dir),
            patch.object(notify_handler, "ENABLED_FLAG", state_dir / "codex-enabled"),
            patch.object(sys, "argv", ["codex-matrix-notify", json.dumps(payload)]),
            patch.dict(os.environ, {"TMUX_PANE": "%0"}),
            patch.object(notify_handler, "find_session_file", return_value=path),
            patch.object(notify_handler, "_ensure_daemon_running", lambda: None),
        ):
            notify_handler.handle_notify()

        return state_dir

    def test_an_exec_run_never_signals_the_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = _rollout(tmp, originator="codex_exec", source="exec")
            state_dir = self._run_notify(tmp, path, {"originator": "codex_exec"})
            self.assertFalse((state_dir / "codex-notify-signal").exists())

    def test_the_opt_in_wins_even_when_the_payload_says_exec(self):
        # The payload and the file are checked redundantly on purpose, so the
        # opt-in has to clear both — this is the case that regressed easily.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = _rollout(
                tmp, originator="codex_exec", source="exec",
                prompt=f"{FORCE_MARKER} summarise",
            )
            state_dir = self._run_notify(tmp, path, {"originator": "codex_exec", "source": "exec"})
            self.assertTrue((state_dir / "codex-notify-signal").exists())

    def test_an_interactive_session_still_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            path = _rollout(tmp, originator="codex-tui", source="cli")
            state_dir = self._run_notify(tmp, path, {"originator": "codex-tui"})
            self.assertTrue((state_dir / "codex-notify-signal").exists())


if __name__ == "__main__":
    unittest.main()
