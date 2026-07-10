"""Regression: all three daemons ignore m.audio events entirely.

Voice now arrives as ordinary @admin text via the server-side voicehub STT
appservice, so the VM-side daemons must not have (or reach) any audio path.
"""

import unittest
from types import SimpleNamespace

from antigravity_matrix.daemon import AntigravityDaemon
from claude_code_matrix.daemon import InboundDaemon
from codex_matrix.daemon import CodexDaemon


AUDIO_EVENT = {
    "type": "m.room.message",
    "sender": "@me:hs",
    "content": {"msgtype": "m.audio", "url": "mxc://hs/abc", "body": "voice.ogg"},
}

TEXT_EVENT = {
    "type": "m.room.message",
    "sender": "@me:hs",
    "content": {"msgtype": "m.text", "body": "hi there"},
}


class _Spy:
    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, *args):
        self.calls.append(args)


class DaemonsIgnoreAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_ignores_audio_but_routes_text(self):
        daemon = InboundDaemon.__new__(InboundDaemon)
        daemon.config = SimpleNamespace(user_id="@bot:hs")
        spy = _Spy()
        daemon._on_text = spy

        self.assertFalse(hasattr(daemon, "_on_audio"))
        await daemon._handle_event("!r:hs", AUDIO_EVENT)
        self.assertEqual(spy.calls, [])

        await daemon._handle_event("!r:hs", TEXT_EVENT)
        self.assertEqual(spy.calls, [("!r:hs", "hi there")])

    async def test_codex_ignores_audio_but_routes_text(self):
        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.config = SimpleNamespace(user_id="@bot:hs")
        spy = _Spy()
        daemon._on_inbound_text = spy

        self.assertFalse(hasattr(daemon, "_on_inbound_audio"))
        await daemon._handle_inbound("!r:hs", AUDIO_EVENT)
        self.assertEqual(spy.calls, [])

        await daemon._handle_inbound("!r:hs", TEXT_EVENT)
        self.assertEqual(spy.calls, [("!r:hs", "hi there")])

    async def test_antigravity_ignores_audio_but_routes_text(self):
        daemon = AntigravityDaemon.__new__(AntigravityDaemon)
        daemon.config = SimpleNamespace(user_id="@bot:hs")
        spy = _Spy()
        daemon._on_inbound_text = spy

        self.assertFalse(hasattr(daemon, "_on_inbound_audio"))
        await daemon._handle_inbound("!r:hs", AUDIO_EVENT)
        self.assertEqual(spy.calls, [])

        await daemon._handle_inbound("!r:hs", TEXT_EVENT)
        self.assertEqual(spy.calls, [("!r:hs", "hi there")])


if __name__ == "__main__":
    unittest.main()
