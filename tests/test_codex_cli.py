import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_matrix import cli


# A basename that cannot plausibly exist in the real /usr/local/bin, so the
# fallback tests below can never be satisfied by a script on the host.
PROBE = "notify-codex-matrix-probe.sh"


class CodexCliNotifyInstallTests(unittest.TestCase):
    def test_install_notify_hook_stays_top_level_when_config_ends_in_a_table(self):
        """A bare key appended after `[agents]` belongs to that table, not the root.

        Codex then fails with:
            invalid length 1, expected struct AgentRoleToml with 3 elements in `agents`
        """
        import tomllib

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            config_dir.mkdir(parents=True)
            config = config_dir / "config.toml"
            config.write_text(
                'model = "gpt-5.6-sol"\n'
                "\n"
                "[agents]\n"
                "enabled = true\n"
                'default_subagent_model = "gpt-5.6-terra"\n'
            )

            with patch.dict(os.environ, {"HOME": str(home)}):
                cli._install_notify_hook()

            parsed = tomllib.loads(config.read_text())
            self.assertIn("notify", parsed, "notify must be a top-level key")
            self.assertEqual(
                parsed["notify"],
                [f"{home}/.ccmatrix/codex-notify-wrapper.sh"],
            )
            self.assertEqual(parsed["agents"]["enabled"], True)
            self.assertNotIn("notify", parsed["agents"])

    def test_install_notify_hook_replaces_multi_command_array_with_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            config_dir.mkdir()
            config_path = config_dir / "config.toml"
            config_path.write_text(
                'notify = [ "/usr/local/bin/notify-codex.sh", "/opt/legacy/codex-notify.sh" ]\n'
            )

            with patch.object(cli.Path, "home", return_value=home):
                cli._install_notify_hook()

            content = config_path.read_text()
            self.assertIn(f'notify = [ "{home}/.ccmatrix/codex-notify-wrapper.sh" ]', content)
            self.assertNotIn("/usr/local/bin/notify-codex.sh\", \"", content)

            wrapper = home / ".ccmatrix" / "codex-notify-wrapper.sh"
            matrix_notify = home / ".ccmatrix" / "codex-notify.sh"
            self.assertTrue(os.access(wrapper, os.X_OK))
            self.assertTrue(os.access(matrix_notify, os.X_OK))

            wrapper_text = wrapper.read_text()
            self.assertIn("/usr/local/bin/notify-codex.sh", wrapper_text)
            self.assertIn('"$HOME/.local/bin"/notify-codex.sh', wrapper_text)
            self.assertIn(f'{matrix_notify} "$@"', wrapper_text)

    def test_install_notify_hook_preserves_existing_wrapper_passthrough(self):
        """A wrapper written before the marker existed is still readable."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            state_dir = home / ".ccmatrix"
            config_dir.mkdir()
            state_dir.mkdir()
            wrapper = state_dir / "codex-notify-wrapper.sh"
            matrix_notify = state_dir / "codex-notify.sh"
            config_path = config_dir / "config.toml"
            config_path.write_text(f'notify = [ "{wrapper}" ]\n')
            wrapper.write_text(
                "#!/bin/bash\n"
                '/usr/local/bin/notify-codex.sh "$@" >> /tmp/old.log 2>&1 || true\n'
                f'{matrix_notify} "$@" >> /tmp/old.log 2>&1 || true\n'
            )

            with patch.object(cli.Path, "home", return_value=home):
                cli._install_notify_hook()

            wrapper_text = wrapper.read_text()
            self.assertIn(
                f"{cli._PASSTHROUGH_MARKER}/usr/local/bin/notify-codex.sh", wrapper_text
            )
            self.assertIn(f'{matrix_notify} "$@"', wrapper_text)

    def test_regenerating_a_wrapper_keeps_the_passthrough(self):
        """Re-running enable must not lose the passthrough it just wrote."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            config_dir.mkdir()
            config_path = config_dir / "config.toml"
            config_path.write_text(f'notify = [ "/usr/local/bin/{PROBE}" ]\n')

            with patch.object(cli.Path, "home", return_value=home):
                cli._install_notify_hook()
                cli._install_notify_hook()
                recovered = cli._existing_wrapper_passthrough_command()

            self.assertEqual(recovered, f"/usr/local/bin/{PROBE}")
            wrapper_text = (home / ".ccmatrix" / "codex-notify-wrapper.sh").read_text()
            self.assertEqual(wrapper_text.count(cli._PASSTHROUGH_MARKER), 1)


class CodexNotifyWrapperFallbackTests(unittest.TestCase):
    """Exercise the generated wrapper as bash actually runs it."""

    def _build(self, home: Path, recorded: Path) -> Path:
        config_dir = home / ".codex"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.toml").write_text(f'notify = [ "{recorded}" ]\n')

        with patch.object(cli.Path, "home", return_value=home):
            cli._install_notify_hook()

        # Stub the Matrix handler: the real one shells into uv. Keeping it in
        # the chain proves the passthrough never blocks the bridge's own leg.
        matrix_notify = home / ".ccmatrix" / "codex-notify.sh"
        matrix_notify.write_text('#!/bin/bash\necho "matrix" >> "$HOME/calls.log"\n')
        matrix_notify.chmod(0o755)
        return home / ".ccmatrix" / "codex-notify-wrapper.sh"

    def _install_fake(self, path: Path, label: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/bash\necho "{label}" >> "$HOME/calls.log"\n')
        path.chmod(0o755)

    def _run(self, wrapper: Path, home: Path) -> list[str]:
        proc = subprocess.run(
            ["bash", str(wrapper), "payload"],
            capture_output=True,
            text=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = home / "calls.log"
        return log.read_text().split() if log.exists() else []

    def test_uses_the_recorded_path_when_it_still_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            recorded = home / "usr-local-bin" / PROBE
            wrapper = self._build(home, recorded)
            self._install_fake(recorded, "recorded")
            self._install_fake(home / ".local" / "bin" / PROBE, "home-local")

            self.assertEqual(self._run(wrapper, home), ["recorded", "matrix"])

    def test_falls_back_to_home_local_bin_when_the_recorded_path_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            recorded = home / "usr-local-bin" / PROBE
            wrapper = self._build(home, recorded)
            # Never created: this is the box-bootstrap relocation.
            self._install_fake(home / ".local" / "bin" / PROBE, "home-local")

            self.assertEqual(self._run(wrapper, home), ["home-local", "matrix"])

    def test_skips_silently_when_no_copy_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            recorded = home / "usr-local-bin" / PROBE
            wrapper = self._build(home, recorded)

            proc = subprocess.run(
                ["bash", str(wrapper), "payload"],
                capture_output=True,
                text=True,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            # The bridge's own leg still runs.
            self.assertEqual((home / "calls.log").read_text().split(), ["matrix"])

    def test_wrapper_without_a_passthrough_is_unchanged_in_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text("model = \"gpt-5\"\n")

            with patch.object(cli.Path, "home", return_value=home):
                cli._install_notify_hook()
                self.assertIsNone(cli._existing_wrapper_passthrough_command())

            wrapper_text = (home / ".ccmatrix" / "codex-notify-wrapper.sh").read_text()
            self.assertNotIn(cli._PASSTHROUGH_MARKER, wrapper_text)
            self.assertNotIn("matrix_passthrough", wrapper_text)


class CodexPassthroughCandidateTests(unittest.TestCase):
    def test_orders_recorded_then_usr_local_then_home_local(self):
        exprs = cli._passthrough_candidate_exprs("/opt/tools/notify.sh")
        self.assertEqual(
            exprs,
            ["/opt/tools/notify.sh", "/usr/local/bin/notify.sh", '"$HOME/.local/bin"/notify.sh'],
        )

    def test_dedupes_when_the_recorded_path_is_already_a_candidate(self):
        exprs = cli._passthrough_candidate_exprs("/usr/local/bin/notify.sh")
        self.assertEqual(
            exprs, ["/usr/local/bin/notify.sh", '"$HOME/.local/bin"/notify.sh']
        )

    def test_quotes_awkward_names(self):
        exprs = cli._passthrough_candidate_exprs("/opt/my tools/notify me.sh")
        self.assertIn("'/opt/my tools/notify me.sh'", exprs)
        self.assertIn('"$HOME/.local/bin"/\'notify me.sh\'', exprs)


if __name__ == "__main__":
    unittest.main()
