"""Bridge-level cc.tts tagging: present on the final assistant message,
absent on non-final sends and when server_side_voice is disabled.

Covered per package via send_messages (Codex + Antigravity share the shape).
The Claude bridge routes the same tts flag through room_send inside
catchup_from_transcript; MatrixClient.room_send tagging is covered in
test_matrix_proxy.py.
"""

import unittest
from types import SimpleNamespace

from antigravity_matrix.bridge import AntigravityBridge
from codex_matrix.bridge import CodexBridge


def _config(server_side_voice=True):
    return SimpleNamespace(
        homeserver="https://hs",
        access_token="tok",
        admin_user_id="@me:hs",
        proxy_url="",
        server_side_voice=server_side_voice,
        repo_aliases={},
    )


class _RoomSendRecorder:
    def __init__(self):
        self.sends: list[dict] = []

    async def room_send(self, room_id, body, catchup=False, tts=False):
        self.sends.append({"body": body, "catchup": catchup, "tts": tts})
        return "$e"


class _SessionStub:
    def __init__(self):
        self._entry = SimpleNamespace(room_id="!r:hs", active=True)

    def get(self, session_id):
        return self._entry


MESSAGES = [
    {"role": "user", "text": "please do the thing"},
    {"role": "assistant", "text": "working on it"},
    {"role": "tool", "text": "● Bash(ls)"},
    {"role": "assistant", "text": "final answer"},
]


class CcTtsTaggingTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, cls, server_side_voice=True):
        bridge = cls(_config(server_side_voice))
        recorder = _RoomSendRecorder()
        bridge.bot_client = recorder
        bridge.session_map = _SessionStub()
        return bridge, recorder

    async def _final_is_tagged(self, cls):
        bridge, rec = self._make(cls, server_side_voice=True)
        await bridge.send_messages("s", list(MESSAGES), notify_final=True)

        # User message is never echoed.
        self.assertNotIn("please do the thing", [s["body"] for s in rec.sends])

        tagged = [s for s in rec.sends if s["tts"]]
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["body"], "final answer")
        self.assertFalse(tagged[0]["catchup"])  # final message notifies (m.text)

    async def _no_tag_when_disabled(self, cls):
        bridge, rec = self._make(cls, server_side_voice=False)
        await bridge.send_messages("s", list(MESSAGES), notify_final=True)
        self.assertEqual([s for s in rec.sends if s["tts"]], [])

    async def _no_tag_without_notify(self, cls):
        bridge, rec = self._make(cls, server_side_voice=True)
        await bridge.send_messages("s", list(MESSAGES), notify_final=False)
        self.assertEqual([s for s in rec.sends if s["tts"]], [])

    async def test_codex_final_is_tagged(self):
        await self._final_is_tagged(CodexBridge)

    async def test_codex_no_tag_when_disabled(self):
        await self._no_tag_when_disabled(CodexBridge)

    async def test_codex_no_tag_without_notify(self):
        await self._no_tag_without_notify(CodexBridge)

    async def test_antigravity_final_is_tagged(self):
        await self._final_is_tagged(AntigravityBridge)

    async def test_antigravity_no_tag_when_disabled(self):
        await self._no_tag_when_disabled(AntigravityBridge)

    async def test_antigravity_no_tag_without_notify(self):
        await self._no_tag_without_notify(AntigravityBridge)


if __name__ == "__main__":
    unittest.main()
