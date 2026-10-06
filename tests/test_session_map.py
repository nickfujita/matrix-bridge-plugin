import tempfile
import unittest
from pathlib import Path

from matrix_bridge.session import SessionMap


class SessionMapRegisterTests(unittest.TestCase):
    def test_register_retires_prior_owner_and_keeps_real_pane_on_provisional_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = SessionMap(Path(tmpdir) / "sessions.json")

            session_map.register("older-session", "%7", "/tmp/older")
            session_map.register("new-session", "%7", "/tmp/new")

            older = session_map.get("older-session")
            newer = session_map.get("new-session")

            self.assertIsNotNone(older)
            self.assertIsNotNone(newer)
            self.assertFalse(older.active)
            self.assertIsNotNone(older.ended_at)
            self.assertTrue(newer.active)
            self.assertEqual(newer.tmux_pane, "%7")

            session_map.register("new-session", "unknown", "/tmp/newer")
            refreshed = session_map.get("new-session")

            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed.tmux_pane, "%7")
            self.assertEqual(refreshed.cwd, "/tmp/newer")
            self.assertTrue(refreshed.active)

    def test_title_failure_backoff_persists_and_clears(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session_map = SessionMap(Path(tmpdir) / "sessions.json")
            session_map.register("session", "%7", "/tmp/project")

            self.assertEqual(
                session_map.record_room_title_failure(
                    "session", now=100, initial_delay=30, max_delay=60
                ),
                30,
            )
            self.assertEqual(
                session_map.record_room_title_failure(
                    "session", now=130, initial_delay=30, max_delay=60
                ),
                60,
            )
            # Re-open the map as a fresh daemon would after restart.
            reloaded = SessionMap(session_map.path)
            entry = reloaded.get("session")
            self.assertEqual(entry.room_title_failure_count, 2)
            self.assertEqual(entry.room_title_retry_at, 190)
            self.assertEqual(reloaded.title_repair_backlog(now=150), (1, 2))

            reloaded.clear_room_title_retry("session")
            self.assertEqual(reloaded.title_repair_backlog(now=150), (0, 0))
