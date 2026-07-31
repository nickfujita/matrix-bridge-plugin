import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = ROOT / "hooks" / "session-sentinel.sh"


def _hook_commands(relative):
    data = json.loads((ROOT / relative).read_text())
    for entries in data.get("hooks", {}).values():
        for matcher in entries:
            for hook in matcher["hooks"]:
                yield hook["command"]


class PluginManifestTests(unittest.TestCase):
    def test_python_hook_commands_run_uv_quietly(self):
        # uv chatter on stdout would corrupt the JSON a hook returns, so every
        # hook that shells into the workspace must go through `uv run --quiet`.
        for command in _hook_commands("hooks/hooks.json"):
            if "python -m" not in command:
                continue
            self.assertIn("uv run --quiet --project", command)

    def test_hook_commands_stay_inside_the_plugin(self):
        for command in _hook_commands("hooks/hooks.json"):
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", command)

    def test_hooks_are_declared_in_exactly_one_place(self):
        """Claude Code loads hooks/hooks.json automatically *and* merges
        plugin.json's inline `hooks` on top of it — its manifest schema calls
        that field "additional hooks (in addition to those in hooks/hooks.json,
        if it exists)". Declaring a hook in both files runs it twice per event,
        which double-posted every tool one-liner to Matrix.
        """
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        self.assertNotIn("hooks", plugin)

        declared = list(_hook_commands("hooks/hooks.json"))
        self.assertEqual(sorted(declared), sorted(set(declared)))

    def test_stop_hook_keeps_a_long_timeout(self):
        # The Stop hook drains the transcript to Matrix, chunked, over the
        # network. plugin.json allowed 600s while hooks.json allowed 15s;
        # consolidating on hooks.json must not quietly adopt the short one.
        data = json.loads((ROOT / "hooks/hooks.json").read_text())
        timeouts = [
            hook["timeout"]
            for matcher in data["hooks"]["Stop"]
            for hook in matcher["hooks"]
        ]
        self.assertEqual(timeouts, [600])

    def test_plugin_and_marketplace_versions_match(self):
        plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        entry = next(
            p for p in marketplace["plugins"] if p["name"] == plugin["name"]
        )
        self.assertEqual(plugin["version"], entry["version"])


class SessionSentinelTests(unittest.TestCase):
    """The sentinel is the only hook the model actually reads, so pin its shape."""

    def test_declared_once_and_only_in_hooks_json(self):
        # See test_hooks_are_declared_in_exactly_one_place: a second declaration
        # would inject the same context twice per session.
        in_hooks_json = [c for c in _hook_commands("hooks/hooks.json") if "session-sentinel" in c]
        in_plugin_json = [
            c for c in _hook_commands(".claude-plugin/plugin.json") if "session-sentinel" in c
        ]
        self.assertEqual(len(in_hooks_json), 1)
        self.assertEqual(in_plugin_json, [])

    def test_registered_on_session_start(self):
        data = json.loads((ROOT / "hooks/hooks.json").read_text())
        commands = [
            hook["command"]
            for matcher in data["hooks"]["SessionStart"]
            for hook in matcher["hooks"]
        ]
        self.assertTrue(any("session-sentinel" in c for c in commands))

    def test_is_executable(self):
        self.assertTrue(SENTINEL.stat().st_mode & 0o111)

    def _run(self, home):
        proc = subprocess.run(
            ["bash", str(SENTINEL)],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_emits_additional_context_when_a_bridge_is_enabled(self):
        for flag in ("enabled", "codex-enabled"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as home:
                state = Path(home) / ".ccmatrix"
                state.mkdir()
                (state / flag).touch()

                output = self._run(home)
                specific = output["hookSpecificOutput"]
                self.assertEqual(specific["hookEventName"], "SessionStart")
                context = specific["additionalContext"]
                self.assertIn("go-mobile", context)
                self.assertIn("Matrix phone bridge", context)
                self.assertIn("tmux pane", context)

    def test_stays_silent_when_no_bridge_is_enabled(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self._run(home), {})

    def test_context_stays_cheap(self):
        with tempfile.TemporaryDirectory() as home:
            state = Path(home) / ".ccmatrix"
            state.mkdir()
            (state / "enabled").touch()
            context = self._run(home)["hookSpecificOutput"]["additionalContext"]
        # Injected into every single session — keep it to a couple of lines.
        self.assertLessEqual(len(context), 600)


if __name__ == "__main__":
    unittest.main()
