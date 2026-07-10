import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginManifestTests(unittest.TestCase):
    def test_claude_hook_commands_run_uv_quietly(self):
        for relative in [".claude-plugin/plugin.json", "hooks/hooks.json"]:
            data = json.loads((ROOT / relative).read_text())
            with self.subTest(relative=relative):
                for entries in data["hooks"].values():
                    for matcher in entries:
                        for hook in matcher["hooks"]:
                            command = hook["command"]
                            self.assertIn("uv run --quiet --project", command)


if __name__ == "__main__":
    unittest.main()
