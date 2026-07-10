"""MatrixClient proxy passthrough + cc.tts / cc.catchup content-flag tests."""

import unittest

from matrix_bridge.matrix import MatrixClient


class _FakeResp:
    def __init__(self, data):
        self.status = 200
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._data

    async def read(self):
        return b""

    async def text(self):
        return ""


class _FakeSession:
    """Records the kwargs of every request without touching the network."""

    def __init__(self, resp_data=None):
        self.calls: list[dict] = []
        self._resp_data = resp_data or {}

    def _record(self, method, url, json=None, params=None, data=None, proxy=None, **kw):
        self.calls.append({"method": method, "url": url, "json": json, "params": params, "proxy": proxy})
        return _FakeResp(self._resp_data)

    def put(self, url, **kw):
        return self._record("put", url, **kw)

    def get(self, url, **kw):
        return self._record("get", url, **kw)

    def post(self, url, **kw):
        return self._record("post", url, **kw)


class ProxyAndTaggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_send_uses_proxy_and_tags_cc_tts(self):
        client = MatrixClient("https://hs", "tok", proxy="http://127.0.0.1:1055")
        client.session = _FakeSession({"event_id": "$e"})

        await client.room_send("!r:hs", "final answer", tts=True)

        call = client.session.calls[-1]
        self.assertEqual(call["proxy"], "http://127.0.0.1:1055")
        self.assertIs(call["json"]["cc.tts"], True)
        self.assertEqual(call["json"]["msgtype"], "m.text")

    async def test_room_send_without_tts_has_no_cc_tts_and_no_proxy(self):
        client = MatrixClient("https://hs", "tok")  # no proxy
        client.session = _FakeSession({"event_id": "$e"})

        await client.room_send("!r:hs", "progress update")

        call = client.session.calls[-1]
        self.assertIsNone(call["proxy"])
        self.assertNotIn("cc.tts", call["json"])

    async def test_catchup_send_is_notice_and_not_tts(self):
        client = MatrixClient("https://hs", "tok")
        client.session = _FakeSession({"event_id": "$e"})

        await client.room_send("!r:hs", "silent progress", catchup=True)

        call = client.session.calls[-1]
        self.assertEqual(call["json"]["msgtype"], "m.notice")
        self.assertIs(call["json"]["cc.catchup"], True)
        self.assertNotIn("cc.tts", call["json"])

    async def test_sync_join_and_typing_pass_proxy(self):
        client = MatrixClient("https://hs", "tok", proxy="http://127.0.0.1:1055")

        client.session = _FakeSession({"next_batch": "s2"})
        await client.sync(timeout=0)
        self.assertEqual(client.session.calls[-1]["method"], "get")
        self.assertEqual(client.session.calls[-1]["proxy"], "http://127.0.0.1:1055")

        client.session = _FakeSession({})
        await client.room_join("!r:hs")
        self.assertEqual(client.session.calls[-1]["method"], "post")
        self.assertEqual(client.session.calls[-1]["proxy"], "http://127.0.0.1:1055")

        client.session = _FakeSession({})
        await client.room_typing("!r:hs", "@u:hs", typing=True)
        self.assertEqual(client.session.calls[-1]["method"], "put")
        self.assertEqual(client.session.calls[-1]["proxy"], "http://127.0.0.1:1055")

    async def test_empty_proxy_string_becomes_none(self):
        client = MatrixClient("https://hs", "tok", proxy="")
        self.assertIsNone(client.proxy)
        client.session = _FakeSession({})
        await client.download("mxc://hs/media123")
        self.assertIsNone(client.session.calls[-1]["proxy"])


if __name__ == "__main__":
    unittest.main()
