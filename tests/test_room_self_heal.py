"""Regression tests: a stale/unreachable room must not silently swallow messages.

Reproduces the failure seen in production: a session's persisted room_id pointed
at a room the (new, scoped) bot account was not a member of. Every send returned
403, but synced_message_count advanced anyway, so the messages were marked synced
and lost forever, silently.
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from matrix_bridge.matrix import RoomUnavailable


class RoomUnavailableTests(unittest.TestCase):
    def test_carries_room_id(self):
        exc = RoomUnavailable("!dead:example.com", "M_FORBIDDEN")
        self.assertEqual(exc.room_id, "!dead:example.com")
        self.assertIn("M_FORBIDDEN", str(exc))


class _FakeSessionMap:
    """Minimal stand-in for SessionMap with the bits the bridge touches."""

    def __init__(self, entry):
        self._entry = entry
        self.synced_count = None

    def get(self, session_id):
        return self._entry

    def set_room_id(self, session_id, room_id):
        self._entry.room_id = room_id

    def set_synced_count(self, session_id, count):
        self.synced_count = count

    def set_last_branch(self, session_id, branch):
        pass


class _Entry:
    def __init__(self, room_id, synced=10):
        self.room_id = room_id
        self.cwd = "/tmp/repo"
        self.active = True
        self.synced_message_count = synced
        self.last_branch = "main"


class CodexSelfHealTests(unittest.IsolatedAsyncioTestCase):
    """CodexBridge.send_messages is the simplest send path to exercise."""

    async def _bridge(self, send_side_effect):
        from codex_matrix.bridge import CodexBridge

        cfg = MagicMock()
        cfg.server_side_voice = True
        cfg.repo_aliases = {}
        bridge = CodexBridge.__new__(CodexBridge)  # skip __init__ (no network)
        bridge.config = cfg
        bridge.bot_client = MagicMock()
        bridge.bot_client.room_send = AsyncMock(side_effect=send_side_effect)
        bridge.session_map = _FakeSessionMap(_Entry("!stale:example.com"))
        bridge.create_room = AsyncMock(
            side_effect=lambda sid, cwd: bridge.session_map.set_room_id(sid, "!fresh:example.com")
        )
        return bridge

    async def test_unavailable_room_is_recreated_and_message_resent(self):
        calls = []

        async def send(room_id, text, **kw):
            calls.append(room_id)
            if room_id == "!stale:example.com" and kw.get("raise_on_unavailable"):
                raise RoomUnavailable(room_id, "M_FORBIDDEN")
            return "$evt"

        bridge = await self._bridge(send)
        sent = await bridge.send_messages(
            "s1", [{"role": "assistant", "text": "hello"}], notify_final=True
        )

        self.assertEqual(sent, 1, "the message must be counted as sent")
        bridge.create_room.assert_awaited_once()
        self.assertEqual(
            calls, ["!stale:example.com", "!fresh:example.com"],
            "must retry into the freshly created room",
        )
        self.assertEqual(bridge.session_map._entry.room_id, "!fresh:example.com")

    async def test_healthy_room_is_not_recreated(self):
        async def send(room_id, text, **kw):
            return "$evt"

        bridge = await self._bridge(send)
        bridge.session_map = _FakeSessionMap(_Entry("!good:example.com"))
        bridge.create_room = AsyncMock()

        sent = await bridge.send_messages("s1", [{"role": "assistant", "text": "hi"}])
        self.assertEqual(sent, 1)
        bridge.create_room.assert_not_awaited()

    async def test_persistently_unavailable_does_not_loop(self):
        """If even the fresh room fails, give up — never spin creating rooms."""

        async def send(room_id, text, **kw):
            if kw.get("raise_on_unavailable"):
                raise RoomUnavailable(room_id, "M_FORBIDDEN")
            raise RoomUnavailable(room_id, "M_FORBIDDEN")

        bridge = await self._bridge(send)
        with self.assertRaises(RoomUnavailable):
            # the retry send (no raise_on_unavailable) still raises here because the
            # fake always raises; real room_send would return None. Either way the
            # loop must not create more than one replacement room.
            await bridge.send_messages("s1", [{"role": "assistant", "text": "x"}])
        self.assertEqual(bridge.create_room.await_count, 1)


if __name__ == "__main__":
    unittest.main()
