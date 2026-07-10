import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class SyncToGlobalTests(unittest.TestCase):
    def test_sync_dry_run_does_not_write_global_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()

            result = subprocess.run(
                ["bash", "scripts/sync-to-global.sh", "--dry-run"],
                cwd=REPO_ROOT,
                env={**os.environ, "HOME": str(home)},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRY-RUN", result.stdout)
            self.assertFalse((home / ".claude" / "plugins" / "cache").exists())

    def test_sync_updates_installed_plugin_and_runs_codex_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            bin_dir = root / "bin"
            home.mkdir()
            bin_dir.mkdir()

            uv_log = root / "uv.log"
            (bin_dir / "uv").write_text(
                "#!/bin/bash\n"
                f"printf '%s\\n' \"$*\" >> {uv_log}\n"
            )
            (bin_dir / "uv").chmod(0o755)

            version = json.loads((REPO_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
            installed = home / ".claude" / "plugins" / "installed_plugins.json"
            installed.parent.mkdir(parents=True)
            installed.write_text(json.dumps({"plugins": {"claude-code-matrix@claude-code-matrix": [{}]}}))

            result = subprocess.run(
                ["bash", "scripts/sync-to-global.sh", "--no-restart", "--no-refresh-rooms"],
                cwd=REPO_ROOT,
                env={**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("DeprecationWarning", result.stderr)

            cache_path = home / ".claude" / "plugins" / "cache" / "claude-code-matrix" / "claude-code-matrix" / version
            self.assertTrue((cache_path / ".claude-plugin" / "plugin.json").exists())
            self.assertFalse((cache_path / ".git").exists())

            data = json.loads(installed.read_text())
            entry = data["plugins"]["claude-code-matrix@claude-code-matrix"][0]
            self.assertEqual(entry["installPath"], str(cache_path))
            self.assertEqual(entry["version"], version)
            self.assertTrue(entry["gitCommitSha"])
            self.assertIn("lastUpdated", entry)

            self.assertIn(f"run --project {cache_path} codex-matrix enable", uv_log.read_text())


if __name__ == "__main__":
    unittest.main()
