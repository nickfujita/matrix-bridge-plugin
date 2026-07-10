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
