"""Hooks must survive a homeserver outage without noise or lost messages.

Reproduces the failure seen on cloud-dev-2: the box hosting the homeserver was
switched off, the Stop hook blocked for the full 60s client timeout, then died
with a traceback that Claude Code printed as "Stop hook error". Nothing was
lost (the transcript cursor had not advanced), but every later hook stalled the
same way until the homeserver came back.
"""

import asyncio
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from matrix_bridge.backoff import (
    INITIAL_DELAY,
    MAX_DELAY,
    HomeserverBreaker,
    HomeserverUnreachable,
)


class HomeserverBreakerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.breaker = HomeserverBreaker(Path(self.tmp.name) / "state" / "backoff.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_closed_by_default(self):
        self.assertEqual(self.breaker.retry_in(now=1000), 0)
        self.breaker.check(now=1000)  # no raise

    def test_first_failure_opens_for_initial_delay(self):
        self.assertEqual(self.breaker.record_failure(now=1000), INITIAL_DELAY)
        with self.assertRaises(HomeserverUnreachable) as ctx:
            self.breaker.check(now=1000)
        self.assertAlmostEqual(ctx.exception.retry_in, INITIAL_DELAY)
        self.breaker.check(now=1000 + INITIAL_DELAY)  # window elapsed: retry allowed

    def test_concurrent_failures_do_not_compound(self):
        """Several hooks failing in the same window are one outage, not many."""
        self.breaker.record_failure(now=1000)
        self.breaker.record_failure(now=1001)
        self.breaker.record_failure(now=1002)
        self.assertAlmostEqual(self.breaker.retry_in(now=1002), INITIAL_DELAY - 2)

    def test_failure_after_window_doubles_up_to_cap(self):
        now = 1000.0
        delay = self.breaker.record_failure(now=now)
        seen = [delay]
        for _ in range(6):
            now += delay
            delay = self.breaker.record_failure(now=now)
            seen.append(delay)
        self.assertEqual(seen[:4], [30, 60, 120, 240])
        self.assertEqual(seen[4:], [MAX_DELAY] * 3)

    def test_success_closes(self):
        self.breaker.record_failure(now=1000)
        self.breaker.record_success()
        self.breaker.check(now=1000)
        self.breaker.record_success()  # idempotent when already closed

    def test_corrupt_state_file_is_treated_as_closed(self):
        self.breaker.path.parent.mkdir(parents=True)
        self.breaker.path.write_text("not json")
        self.breaker.check(now=1000)


def _bridge(breaker):
    from claude_code_matrix.bridge import MatrixBridge

    bridge = MatrixBridge.__new__(MatrixBridge)  # skip __init__ (no network)
    bridge.config = MagicMock(server_side_voice=False, repo_aliases={})
    bridge.bot_client = MagicMock()
    bridge.bot_client.__aenter__ = AsyncMock()
    bridge.bot_client.__aexit__ = AsyncMock()
    bridge.bot_client.room_send = AsyncMock(return_value="$evt")
    bridge.breaker = breaker
    return bridge


class BridgeBreakerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.breaker = HomeserverBreaker(Path(self.tmp.name) / "backoff.json")

    def tearDown(self):
        self.tmp.cleanup()

    async def test_transient_error_opens_breaker(self):
        bridge = _bridge(self.breaker)
        with self.assertRaises(asyncio.TimeoutError):
            async with bridge:
                raise asyncio.TimeoutError()
        self.assertGreater(self.breaker.retry_in(), 0)

    async def test_connection_error_opens_breaker(self):
        bridge = _bridge(self.breaker)
        with self.assertRaises(aiohttp.ClientError):
            async with bridge:
                raise aiohttp.ClientConnectionError("refused")
        self.assertGreater(self.breaker.retry_in(), 0)

    async def test_open_breaker_skips_without_opening_a_session(self):
        self.breaker.record_failure()
        bridge = _bridge(self.breaker)
        with self.assertRaises(HomeserverUnreachable):
            async with bridge:
                self.fail("body must not run while the breaker is open")
        bridge.bot_client.__aenter__.assert_not_awaited()

    async def test_clean_exit_closes_breaker(self):
        self.breaker.record_failure(now=0)  # long expired: a probe is allowed
        bridge = _bridge(self.breaker)
        async with bridge:
            pass
        self.assertEqual(self.breaker.retry_in(), 0)

    async def test_programming_errors_do_not_open_breaker(self):
        """A bug in a handler is not an outage; it must still surface loudly."""
        bridge = _bridge(self.breaker)
        with self.assertRaises(KeyError):
            async with bridge:
                raise KeyError("room_id")
        self.assertEqual(self.breaker.retry_in(), 0)


class RunHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, exc):
        from claude_code_matrix.hooks import run_handler

        async def handler(payload):
            raise exc

        err = io.StringIO()
        with redirect_stderr(err):
            result = await run_handler("stop", handler, {})
        return result, err.getvalue()

    async def test_unreachable_is_a_quiet_no_op(self):
        result, err = await self._run(HomeserverUnreachable(42))
        self.assertEqual(result, {})
        self.assertEqual(err.count("\n"), 1, "exactly one line, not a traceback")
        self.assertIn("stop skipped", err)

    async def test_timeout_is_a_quiet_no_op(self):
        result, err = await self._run(asyncio.TimeoutError())
        self.assertEqual(result, {})
        self.assertEqual(err.count("\n"), 1)
        self.assertIn("could not reach the homeserver", err)

    async def test_other_errors_still_propagate(self):
        with self.assertRaises(RuntimeError):
            await self._run(RuntimeError("bug"))


class _FakeSessionMap:
    def __init__(self, entry):
        self._entry = entry
        self.counts = []

    def get(self, session_id):
        return self._entry

    def set_synced_count(self, session_id, count):
        self._entry.synced_message_count = count
        self.counts.append(count)


class _Entry:
    def __init__(self, synced=0):
        self.room_id = "!room:example.com"
        self.cwd = "/tmp/repo"
        self.active = True
        self.synced_message_count = synced
        self.last_branch = "main"


MESSAGES = [
    {"role": "user", "text": "q1"},
    {"role": "assistant", "text": "a1"},
    {"role": "assistant", "text": "a2"},
    {"role": "user", "text": "q2"},
    {"role": "assistant", "text": "a3"},
]


class CatchupCursorTests(unittest.IsolatedAsyncioTestCase):
    """The cursor must track delivered messages one at a time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entry = _Entry()
        self.bridge = _bridge(HomeserverBreaker(Path(self.tmp.name) / "backoff.json"))
        self.bridge.session_map = _FakeSessionMap(self.entry)
        self.bridge.refresh_branch_if_changed = AsyncMock()
        patches = [
            patch("claude_code_matrix.bridge.STATE_DIR", Path(self.tmp.name)),
            patch("claude_code_matrix.transcript.find_transcript", return_value="t.jsonl"),
            patch("claude_code_matrix.transcript.extract_messages", return_value=MESSAGES),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_mid_batch_failure_resumes_at_first_undelivered(self):
        sent = []
        outage = {"a2"}  # the homeserver dies exactly once, on a2

        async def send(room_id, text, **kw):
            if text in outage:
                outage.remove(text)
                raise asyncio.TimeoutError()
            sent.append(text)
            return "$evt"

        self.bridge.bot_client.room_send = AsyncMock(side_effect=send)

        with self.assertRaises(asyncio.TimeoutError):
            await self.bridge.catchup_from_transcript("s1")
        self.assertEqual(self.entry.synced_message_count, 2, "a1 delivered, a2 not")

        # Homeserver is back: the retry starts at a2, never resending a1.
        synced = await self.bridge.catchup_from_transcript("s1", notify_final=True)
        self.assertEqual(synced, 3)
        self.assertEqual(sent, ["a1", "a2", "a3"])
        self.assertEqual(self.entry.synced_message_count, len(MESSAGES))

    async def test_final_notification_goes_to_newest_message_after_replay(self):
        kinds = {}

        async def send(room_id, text, **kw):
            kinds[text] = kw.get("catchup")
            return "$evt"

        self.bridge.bot_client.room_send = AsyncMock(side_effect=send)
        await self.bridge.catchup_from_transcript("s1", notify_final=True)
        self.assertEqual(kinds, {"a1": True, "a2": True, "a3": False})


if __name__ == "__main__":
    unittest.main()
