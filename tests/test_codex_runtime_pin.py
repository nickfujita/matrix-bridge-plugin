"""Generated runtime resolution uses temporary checkouts; no live services."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_matrix.cli import _notify_script_updates, _plugin_root_resolution_lines


class RuntimePinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.source = self.checkout(self.home / "stable checkout", "0.8.4")
        self.cache = self.checkout(
            self.home / ".codex/plugins/cache/matrix-bridge-plugin/matrix-bridge-plugin/0.8.1",
            "0.8.1",
        )
        self.pin = self.home / ".ccmatrix/runtime-root"
        self.pin.parent.mkdir()

    def checkout(self, path, version):
        marker = path / "packages/codex-matrix/src/codex_matrix/notify_handler.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("")
        manifest = path / ".claude-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"name": "matrix-bridge-plugin", "version": version}))
        return path

    def resolve(self, **env):
        with patch("codex_matrix.cli.Path.home", return_value=self.home):
            lines = _plugin_root_resolution_lines(self.cache)
        clean_env = {k: v for k, v in os.environ.items() if k != "CCMATRIX_RUNTIME_ROOT"}
        return subprocess.run(
            ["bash", "-c", "\n".join(lines) + '\nprintf "%s\\n" "$matrix_root"'],
            env={**clean_env, "HOME": str(self.home), **env},
            text=True, capture_output=True, timeout=10,
        )

    def test_source_pin_wins_over_installed_cache_and_survives_reenable(self):
        self.pin.write_text(str(self.source) + "\n")
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.source))

    def test_invalid_pin_fails_without_silent_cache_fallback(self):
        self.pin.write_text(str(self.home / "removed checkout"))
        result = self.resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("refusing a cache downgrade", result.stderr)

    def test_symlink_pin_file_is_rejected(self):
        target = self.home / "pin.txt"
        target.write_text(str(self.source))
        self.pin.symlink_to(target)
        result = self.resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a symlink", result.stderr)

    def test_environment_pin_overrides_file_pin(self):
        self.pin.write_text(str(self.home / "removed checkout"))
        result = self.resolve(CCMATRIX_RUNTIME_ROOT=str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.source))

    def test_unpinned_install_keeps_normal_cache_resolution(self):
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.cache))

    def test_notify_script_uses_module_without_dependency_resynchronization(self):
        with patch("codex_matrix.cli.Path.home", return_value=self.home):
            updates = _notify_script_updates(None)
        content = updates[0][1].decode()
        self.assertIn("uv run --no-sync --quiet python -m codex_matrix.notify_handler", content)
        self.assertNotIn("notify called with:", content)
