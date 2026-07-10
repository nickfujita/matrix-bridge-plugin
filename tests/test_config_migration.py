"""Config migration + hardening tests for the 0.5.0 breaking-config release."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from matrix_bridge import config as cfg


def _no_ccmatrix_env() -> dict:
    """Environment copy with all CCMATRIX_* overrides stripped.

    Ensures load_config() takes the file path, not the env path.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("CCMATRIX_")}


class ConfigMigrationTests(unittest.TestCase):
    def _load_from(self, cfile: Path):
        with patch.object(cfg, "CONFIG_DIR", cfile.parent), \
             patch.object(cfg, "CONFIG_FILE", cfile), \
             patch.dict(os.environ, _no_ccmatrix_env(), clear=True):
            return cfg.load_config()

    def test_legacy_keys_dropped_resaved_and_secured(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfile = Path(tmp) / "config.json"
            cfile.write_text(json.dumps({
                "homeserver": "https://hs.example",
                "user_id": "@mybot-a:hs.example",
                "access_token": "bot-token",
                "admin_user_id": "@me:hs.example",
                "admin_access_token": "SUPER-SECRET-ADMIN",
                "voice_service_url": "http://127.0.0.1:7239",
                "device_id": "CCMATRIX",
            }))
            cfile.chmod(0o644)

            config = self._load_from(cfile)

            self.assertIsNotNone(config)
            self.assertEqual(config.admin_user_id, "@me:hs.example")
            self.assertTrue(config.server_side_voice)  # default
            self.assertEqual(config.proxy_url, "")
            # Removed fields no longer exist on the dataclass at all.
            self.assertFalse(hasattr(config, "admin_access_token"))
            self.assertFalse(hasattr(config, "voice_service_url"))

            # File was re-saved cleanly (migration shim).
            data = json.loads(cfile.read_text())
            self.assertNotIn("admin_access_token", data)
            self.assertNotIn("voice_service_url", data)
            self.assertIn("admin_user_id", data)

            # Permissions tightened to 0600.
            self.assertEqual(stat.S_IMODE(cfile.stat().st_mode), 0o600)

    def test_clean_config_still_gets_chmod_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfile = Path(tmp) / "config.json"
            cfile.write_text(json.dumps({
                "homeserver": "https://hs.example",
                "user_id": "@mybot-a:hs.example",
                "access_token": "bot-token",
                "admin_user_id": "@me:hs.example",
                "device_id": "CCMATRIX",
                "server_side_voice": False,
                "proxy_url": "http://127.0.0.1:1055",
            }))
            cfile.chmod(0o644)

            config = self._load_from(cfile)

            self.assertIsNotNone(config)
            self.assertFalse(config.server_side_voice)
            self.assertEqual(config.proxy_url, "http://127.0.0.1:1055")
            self.assertEqual(stat.S_IMODE(cfile.stat().st_mode), 0o600)

    def test_save_config_writes_secure_and_without_legacy_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfile = Path(tmp) / "config.json"
            with patch.object(cfg, "CONFIG_DIR", cfile.parent), \
                 patch.object(cfg, "CONFIG_FILE", cfile):
                cfg.save_config(cfg.MatrixConfig(
                    homeserver="https://hs.example",
                    user_id="@mybot-a:hs.example",
                    access_token="bot-token",
                    admin_user_id="@me:hs.example",
                    proxy_url="http://127.0.0.1:1055",
                ))

            self.assertEqual(stat.S_IMODE(cfile.stat().st_mode), 0o600)
            data = json.loads(cfile.read_text())
            self.assertNotIn("admin_access_token", data)
            self.assertNotIn("voice_service_url", data)
            self.assertEqual(data["proxy_url"], "http://127.0.0.1:1055")
            self.assertIs(data["server_side_voice"], True)
            self.assertEqual(data["admin_user_id"], "@me:hs.example")


if __name__ == "__main__":
    unittest.main()
