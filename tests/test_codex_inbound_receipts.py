import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from codex_matrix.daemon import CodexDaemon
from codex_matrix.inbound_receipts import InboundEventReceipts


class InboundEventReceiptsTests(unittest.TestCase):
    def test_receipt_survives_restart_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbound-events.json"
            receipts = InboundEventReceipts(path, limit=2)
            receipts.record("$first")
            receipts.record("$second")
            receipts.record("$third")

            resumed = InboundEventReceipts(path, limit=2)
            self.assertFalse(resumed.contains("$first"))
            self.assertTrue(resumed.contains("$second"))
            self.assertTrue(resumed.contains("$third"))
            self.assertEqual(resumed.count(), 2)

    def test_malformed_receipt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inbound-events.json"
            path.write_text("not json")

            receipts = InboundEventReceipts(path)

            self.assertFalse(receipts.contains("$event"))
            self.assertEqual(receipts.count(), 0)


class _InboundSessionMap:
    def get_by_room(self, room_id: str):
        if room_id == "!room:test":
            return SimpleNamespace(session_id="thread-1", tmux_pane="%1")
        return None


class _InboundPollClient:
    def __init__(self):
        self.typing_calls: list[tuple] = []

    async def room_typing(self, *args, **kwargs):
        self.typing_calls.append((args, kwargs))


class CodexDaemonInboundDedupTests(unittest.IsolatedAsyncioTestCase):
    def _daemon(self, receipt_path: Path) -> CodexDaemon:
        daemon = CodexDaemon.__new__(CodexDaemon)
        daemon.config = SimpleNamespace(user_id="@bot:test")
        daemon.session_map = _InboundSessionMap()
        daemon.poll_client = _InboundPollClient()
        daemon.inbound_event_receipts = InboundEventReceipts(receipt_path)
        return daemon

    @staticmethod
    def _event(event_id: str) -> dict:
        return {
            "event_id": event_id,
            "type": "m.room.message",
            "sender": "@human:test",
            "content": {"msgtype": "m.text", "body": "test input"},
        }

    async def test_same_matrix_event_is_injected_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = self._daemon(Path(tmp) / "receipts.json")
            with patch("codex_matrix.daemon.send_keys", new=AsyncMock(return_value=True)) as send:
                await daemon._handle_inbound("!room:test", self._event("$event"))
                await daemon._handle_inbound("!room:test", self._event("$event"))

            send.assert_awaited_once_with("%1", "test input")
            self.assertEqual(len(daemon.poll_client.typing_calls), 1)
            # The durable state contains only Matrix IDs, never user text.
            self.assertNotIn("test input", (Path(tmp) / "receipts.json").read_text())

    async def test_failed_injection_is_not_receipted_and_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            daemon = self._daemon(Path(tmp) / "receipts.json")
            with patch(
                "codex_matrix.daemon.send_keys",
                new=AsyncMock(side_effect=[False, True]),
            ) as send:
                await daemon._handle_inbound("!room:test", self._event("$event"))
                await daemon._handle_inbound("!room:test", self._event("$event"))

            self.assertEqual(send.await_count, 2)
            self.assertEqual(len(daemon.poll_client.typing_calls), 1)
            self.assertTrue(daemon.inbound_event_receipts.contains("$event"))

    async def test_receipt_prevents_replay_after_daemon_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.json"
            first = self._daemon(path)
            with patch("codex_matrix.daemon.send_keys", new=AsyncMock(return_value=True)) as first_send:
                await first._handle_inbound("!room:test", self._event("$event"))
            first_send.assert_awaited_once()

            resumed = self._daemon(path)
            with patch("codex_matrix.daemon.send_keys", new=AsyncMock(return_value=True)) as resumed_send:
                await resumed._handle_inbound("!room:test", self._event("$event"))
            resumed_send.assert_not_awaited()
            self.assertEqual(len(resumed.poll_client.typing_calls), 0)

class SyncCursorTests(unittest.TestCase):
    def test_cursor_is_usable_only_for_a_short_handoff(self):
        from codex_matrix.sync_cursor import SyncCursor

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cursor.json"
            cursor = SyncCursor(path, max_age_seconds=10)
            with patch("codex_matrix.sync_cursor.time.time", return_value=100):
                cursor.save("next-token")
            self.assertEqual(cursor.load_fresh(now=109), "next-token")
            self.assertIsNone(cursor.load_fresh(now=111))


class _SyncPollClient:
    def __init__(self, responses: list[dict]):
        self.responses = iter(responses)
        self.calls: list[dict] = []

    async def sync(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)

    async def reconnect(self):
        raise AssertionError("reconnect is not expected")


class CodexDaemonSyncCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_batch_cursor_is_resumed_after_restart(self):
        from codex_matrix.sync_cursor import SyncCursor

        with tempfile.TemporaryDirectory() as tmp:
            cursor_path = Path(tmp) / "cursor.json"
            first = CodexDaemon.__new__(CodexDaemon)
            first.running = True
            first.sync_cursor = SyncCursor(cursor_path)
            first.poll_client = _SyncPollClient([
                {"next_batch": "initial"},
                {"next_batch": "completed"},
            ])

            async def first_process(_data):
                first.running = False

            first._process_sync = first_process
            await first._matrix_poll_loop()
            self.assertEqual(first.poll_client.calls, [
                {"timeout": 10000},
                {"since": "initial", "timeout": 30000},
            ])
            self.assertEqual(first.sync_cursor.load_fresh(), "completed")

            resumed = CodexDaemon.__new__(CodexDaemon)
            resumed.running = True
            resumed.sync_cursor = SyncCursor(cursor_path)
            resumed.poll_client = _SyncPollClient([{"next_batch": "after-resume"}])

            async def resumed_process(_data):
                resumed.running = False

            resumed._process_sync = resumed_process
            await resumed._matrix_poll_loop()
            self.assertEqual(resumed.poll_client.calls, [
                {"since": "completed", "timeout": 30000},
            ])
            self.assertEqual(resumed.sync_cursor.load_fresh(), "after-resume")
