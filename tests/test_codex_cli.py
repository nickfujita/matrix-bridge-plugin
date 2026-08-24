import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock

from codex_matrix import cli, notify_handler


# A basename that cannot plausibly exist in the real /usr/local/bin, so the
# fallback tests below can never be satisfied by a script on the host.
PROBE = "notify-codex-matrix-probe.sh"


class CodexCliNotifyInstallTests(unittest.TestCase):
    def test_failed_reenable_preserves_the_prior_enabled_flag(self):
        """A failed repair attempt cannot turn an already-enabled bridge off."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state_dir = home / ".ccmatrix"
            state_dir.mkdir()
            enabled = state_dir / "codex-enabled"
            enabled.touch()
            config_dir = home / ".codex"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text('model = "gpt-5"\n')

            with (
                patch.object(cli.Path, "home", return_value=home),
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=False),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertTrue(enabled.exists())

    def test_failed_notify_install_restores_config_and_both_live_scripts(self):
        """A late config write failure leaves a live hook byte-for-byte untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            state_dir = home / ".ccmatrix"
            config_dir.mkdir()
            state_dir.mkdir()
            enabled = state_dir / "codex-enabled"
            enabled.touch()
            config_path = config_dir / "config.toml"
            config_path.write_text('notify = [ "/usr/local/bin/notify-codex.sh" ]\n')
            matrix_notify = state_dir / "codex-notify.sh"
            wrapper = state_dir / "codex-notify-wrapper.sh"
            matrix_notify.write_bytes(b"old matrix script\n")
            wrapper.write_bytes(b"old wrapper script\n")
            matrix_notify.chmod(0o711)
            wrapper.chmod(0o741)
            expected = {
                config_path: (config_path.read_bytes(), config_path.stat().st_mode & 0o777),
                matrix_notify: (matrix_notify.read_bytes(), matrix_notify.stat().st_mode & 0o777),
                wrapper: (wrapper.read_bytes(), wrapper.stat().st_mode & 0o777),
            }
            original_replace = cli.os.replace
            fail_once = True

            def fail_config_replace(source: Path, destination: Path):
                nonlocal fail_once
                if destination == config_path and fail_once:
                    fail_once = False
                    raise OSError("simulated config commit failure")
                return original_replace(source, destination)

            with (
                patch.object(cli.Path, "home", return_value=home),
                patch.object(cli.os, "replace", side_effect=fail_config_replace),
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled),
                patch.object(cli, "load_config", return_value=object()),
            ):
                self.assertEqual(cli.cmd_enable(object()), 1)

            for path, (content, mode) in expected.items():
                self.assertEqual(path.read_bytes(), content, path)
                self.assertEqual(path.stat().st_mode & 0o777, mode, path)
            self.assertTrue(enabled.exists())

    def test_install_notify_hook_reports_missing_or_unparseable_config(self):
        """Enable needs an explicit failure instead of a best-effort config mutation."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch.object(cli.Path, "home", return_value=home):
                self.assertIs(cli._install_notify_hook(), False)

            config_dir = home / ".codex"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text("notify = [\n")
            with patch.object(cli.Path, "home", return_value=home):
                self.assertIs(cli._install_notify_hook(), False)

    def test_install_notify_hook_rejects_unsupported_notify_and_write_failure(self):
        """Only a verified, writable top-level notify array can enable Matrix."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            config_dir = home / ".codex"
            config_dir.mkdir()
            config_path = config_dir / "config.toml"
            config_path.write_text('notify = [\n  "/bin/notify"\n]\n')
            with patch.object(cli.Path, "home", return_value=home):
                self.assertIs(cli._install_notify_hook(), False)

            config_path.write_text('model = "gpt-5"\n')
            original_replace = cli.os.replace
            fail_once = True

            def reject_config_commit(source: Path, destination: Path):
                nonlocal fail_once
                if destination == config_path and fail_once:
                    fail_once = False
                    raise OSError("disk full")
                return original_replace(source, destination)

            with (
                patch.object(cli.Path, "home", return_value=home),
                patch.object(cli.os, "replace", side_effect=reject_config_commit),
            ):
                self.assertIs(cli._install_notify_hook(), False)

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
                self.assertTrue(cli._install_notify_hook())

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
                self.assertTrue(cli._install_notify_hook())

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
                self.assertTrue(cli._install_notify_hook())

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
                self.assertTrue(cli._install_notify_hook())
                self.assertTrue(cli._install_notify_hook())
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

    def test_hung_passthrough_does_not_block_the_matrix_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            recorded = home / "usr-local-bin" / PROBE
            wrapper = self._build(home, recorded)
            recorded.parent.mkdir(parents=True)
            recorded.write_text("#!/bin/bash\nsleep 3\n")
            recorded.chmod(0o755)

            started = time.monotonic()
            calls = self._run(wrapper, home)

            self.assertLess(time.monotonic() - started, 2.5)
            self.assertEqual(calls, ["matrix"])

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


class CodexDaemonStartupTests(unittest.TestCase):
    def test_stale_pid_is_removed_when_the_replacement_never_becomes_ready(self):
        """A launcher that exits before publishing a live PID is not a start."""
        class ExitedProcess:
            pid = 424242

            @staticmethod
            def poll():
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            pid_file.write_text("999999")

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch("os.kill", side_effect=ProcessLookupError),
                patch("subprocess.Popen", return_value=ExitedProcess()),
            ):
                from io import StringIO
                from contextlib import redirect_stdout

                output = StringIO()
                with redirect_stdout(output):
                    cli.cmd_start(object())

            self.assertIn("failed", output.getvalue().lower())
            self.assertFalse(pid_file.exists())

    def test_cli_and_notify_do_not_launch_competing_daemons_during_readiness(self):
        """Both entry points must share startup ownership until the PID is live."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            started = threading.Event()
            release_daemon_lock = threading.Event()
            writers: list[threading.Thread] = []
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)

            class StartingProcess:
                pid = os.getpid()

                @staticmethod
                def poll():
                    return None

            def spawn(*_args, **_kwargs):
                started.set()

                def publish_readiness():
                    time.sleep(0.1)
                    daemon_lock.acquire()
                    stat = Path(f"/proc/{os.getpid()}/stat").read_text()
                    pid_file.write_text(json.dumps({
                        "pid": os.getpid(),
                        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                        "start_time": stat[stat.rfind(")") + 2 :].split()[19],
                    }))
                    release_daemon_lock.wait(timeout=2)
                    daemon_lock.release()

                writer = threading.Thread(target=publish_readiness)
                writer.start()
                writers.append(writer)
                return StartingProcess()

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(notify_handler, "STATE_DIR", state_dir),
                patch("subprocess.Popen", side_effect=spawn) as popen,
            ):
                launcher = threading.Thread(target=cli.cmd_start, args=(object(),))
                launcher.start()
                self.assertTrue(started.wait(timeout=1), "CLI did not attempt to launch")
                notify_handler._ensure_daemon_running()
                launcher.join(timeout=2)
                release_daemon_lock.set()

            for writer in writers:
                writer.join(timeout=1)

            self.assertFalse(launcher.is_alive(), "CLI start did not finish")
            self.assertEqual(popen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
