import unittest
from types import SimpleNamespace
from unittest.mock import patch

from antigravity_matrix import hooks


class _BridgeStub:
    instances = []

    def __init__(self, config):
        self.config = config
        self.ensure_calls = []
        self.typing_calls = []
        self.catchup_calls = []
        self.tts_calls = []
        _BridgeStub.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def ensure_room(self, session_id, cwd):
        self.ensure_calls.append((session_id, cwd))

    async def set_typing(self, session_id, typing):
        self.typing_calls.append((session_id, typing))

    async def catchup_from_transcript(self, session_id, transcript, notify_final=False):
        self.catchup_calls.append((session_id, transcript, notify_final))
        return [{"role": "assistant", "text": "finished"}]

    async def send_tts_audio(self, session_id, text):
        self.tts_calls.append((session_id, text))


class AntigravityHookTypingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _BridgeStub.instances = []
        self.patches = [
            patch.object(hooks, "_is_enabled", return_value=True),
            patch.object(hooks, "load_config", return_value=SimpleNamespace()),
            patch.object(hooks, "AntigravityBridge", _BridgeStub),
            patch.object(hooks, "_ensure_daemon_running", return_value=None),
            patch.object(hooks, "_register_session", return_value=("session-1", "/repo")),
        ]
        for patcher in self.patches:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(self.patches)])

    async def test_post_invocation_does_not_reenable_typing(self):
        await hooks.handle_post_invocation({})

        bridge = _BridgeStub.instances[-1]
        self.assertEqual(bridge.ensure_calls, [("session-1", "/repo")])
        self.assertEqual(bridge.typing_calls, [])

    async def test_stop_clears_typing_even_without_transcript(self):
        await hooks.handle_stop({})

        bridge = _BridgeStub.instances[-1]
        self.assertEqual(bridge.ensure_calls, [("session-1", "/repo")])
        self.assertEqual(bridge.catchup_calls, [])
        self.assertEqual(bridge.typing_calls, [("session-1", False)])
        self.assertEqual(bridge.tts_calls, [])


if __name__ == "__main__":
    unittest.main()
