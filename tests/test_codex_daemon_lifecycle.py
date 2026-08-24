import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from filelock import FileLock

from codex_matrix import cli, daemon, daemon_lifecycle, notify_handler


def _current_identity(pid: int) -> dict[str, str | int]:
    """Build a literal Linux process identity without using production helpers."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    start_time = stat[stat.rfind(")") + 2 :].split()[19]
    return {
        "pid": pid,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "start_time": start_time,
    }


class CodexDaemonIdentityTests(unittest.TestCase):
    def test_daemon_writes_a_generation_stable_identity_record(self):
        """The daemon must replace legacy plaintext PID files at its source."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            observed: dict[str, str | int] = {}

            class FastDaemon:
                def __init__(self, _config):
                    self.running = True

                async def start(self):
                    return None

            def observe_then_close(coroutine):
                pid_file = state_dir / "codex-daemon.pid"
                try:
                    record = json.loads(pid_file.read_text())
                    if isinstance(record, dict):
                        observed.update(record)
                except (OSError, json.JSONDecodeError):
                    pass
                coroutine.close()

            with (
                patch.object(daemon, "STATE_DIR", state_dir),
                patch.object(daemon, "load_config", return_value=object()),
                patch.object(daemon, "CodexDaemon", FastDaemon),
                patch.object(daemon.asyncio, "run", side_effect=observe_then_close),
                patch.object(daemon.signal, "signal"),
            ):
                daemon.run_daemon()

            self.assertEqual(observed.get("pid"), os.getpid())
            self.assertEqual(observed.get("boot_id"), _current_identity(os.getpid())["boot_id"])
            self.assertEqual(observed.get("start_time"), _current_identity(os.getpid())["start_time"])

    def test_daemon_signal_handler_requests_async_shutdown(self):
        """The process signal handler must wake runtime tasks, not only flip a flag."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            handlers = {}

            class ShutdownDaemon:
                def __init__(self, _config):
                    self.running = True
                    self.shutdown_requests = 0

                def request_shutdown(self):
                    self.shutdown_requests += 1
                    self.running = False

                async def start(self):
                    return None

            daemon_instance = ShutdownDaemon(object())

            def capture_signal(signum, handler):
                handlers[signum] = handler

            def request_then_close(coroutine):
                handlers[daemon.signal.SIGTERM](daemon.signal.SIGTERM, None)
                coroutine.close()

            with (
                patch.object(daemon, "STATE_DIR", state_dir),
                patch.object(daemon, "load_config", return_value=object()),
                patch.object(daemon, "CodexDaemon", return_value=daemon_instance),
                patch.object(daemon.asyncio, "run", side_effect=request_then_close),
                patch.object(daemon.signal, "signal", side_effect=capture_signal),
            ):
                daemon.run_daemon()

            self.assertEqual(daemon_instance.shutdown_requests, 1)
            self.assertFalse(daemon_instance.running)

    def test_reused_plain_pid_is_replaced_even_when_the_pid_is_alive(self):
        """A post-reboot PID reuse must not suppress the daemon restart."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            pid_file.write_text(str(os.getpid()))
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)

            class Child:
                pid = os.getpid()

                @staticmethod
                def poll():
                    return None

            def spawn(*_args, **_kwargs):
                daemon_lock.acquire()
                pid_file.write_text(json.dumps(_current_identity(os.getpid())))
                return Child()

            try:
                with patch.object(daemon_lifecycle.subprocess, "Popen", side_effect=spawn) as popen:
                    result = daemon_lifecycle.start_daemon(state_dir)
            finally:
                daemon_lock.release()

            self.assertEqual(result.state, "started")
            self.assertEqual(result.pid, os.getpid())
            self.assertEqual(popen.call_count, 1)

    def test_timed_out_child_is_killed_and_cannot_publish_a_late_pid(self):
        """A launch attempt owns shutdown until its child and PID state are gone."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            late_writer_done = threading.Event()

            class StubbornChild:
                pid = 424242
                terminated = False
                killed = False

                def poll(self):
                    return 9 if self.killed else None

                def terminate(self):
                    self.terminated = True

                def wait(self, timeout):
                    if not self.killed:
                        time.sleep(min(timeout, 0.03))
                        raise subprocess.TimeoutExpired("codex_matrix", timeout)
                    return 9

                def kill(self):
                    self.killed = True

                @staticmethod
                def communicate(timeout=None):
                    return b"", b""

            child = StubbornChild()

            def spawn(*_args, **_kwargs):
                def publish_late_pid():
                    time.sleep(0.02)
                    pid_file.write_text(json.dumps(_current_identity(os.getpid())))
                    late_writer_done.set()

                threading.Thread(target=publish_late_pid).start()
                return child

            with (
                patch.object(daemon_lifecycle, "READINESS_TIMEOUT_SECONDS", 0.01),
                patch.object(daemon_lifecycle, "READINESS_POLL_SECONDS", 0.001),
                patch.object(daemon_lifecycle, "TERMINATE_WAIT_SECONDS", 0.05, create=True),
                patch.object(daemon_lifecycle, "KILL_WAIT_SECONDS", 0.05, create=True),
                patch.object(daemon_lifecycle.subprocess, "Popen", side_effect=spawn),
            ):
                result = daemon_lifecycle.start_daemon(state_dir)

            self.assertEqual(result.state, "failed")
            self.assertTrue(late_writer_done.wait(timeout=1))
            self.assertTrue(child.terminated)
            self.assertTrue(child.killed)
            self.assertFalse(pid_file.exists())

    def test_ready_record_from_another_daemon_converges_successfully(self):
        """A losing launcher converges on the winner instead of reporting startup failure."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)

            class LosingChild:
                pid = 424242
                stopped = False

                def poll(self):
                    return 0 if self.stopped else None

                def terminate(self):
                    self.stopped = True

                @staticmethod
                def wait(timeout):
                    return 0

                @staticmethod
                def communicate(timeout=None):
                    return b"", b""

            def spawn(*_args, **_kwargs):
                daemon_lock.acquire()
                pid_file.write_text(json.dumps(_current_identity(os.getpid())))
                return LosingChild()

            try:
                with patch.object(daemon_lifecycle.subprocess, "Popen", side_effect=spawn):
                    result = daemon_lifecycle.start_daemon(state_dir)
            finally:
                daemon_lock.release()

            self.assertEqual(result.state, "already_running")
            self.assertEqual(result.pid, os.getpid())
            self.assertEqual(json.loads(pid_file.read_text()), _current_identity(os.getpid()))

    def test_timeout_preserves_a_record_published_by_another_daemon(self):
        """Late cleanup only owns the launched child, never a replacement daemon."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)
            published = threading.Event()
            release_daemon_lock = threading.Event()

            class StubbornChild:
                pid = 424242
                killed = False

                def poll(self):
                    return 9 if self.killed else None

                @staticmethod
                def terminate():
                    pass

                def wait(self, timeout):
                    if not self.killed:
                        time.sleep(min(timeout, 0.03))
                        raise subprocess.TimeoutExpired("codex_matrix", timeout)
                    return 9

                def kill(self):
                    self.killed = True

                @staticmethod
                def communicate(timeout=None):
                    return b"", b""

            def spawn(*_args, **_kwargs):
                def publish_replacement() -> None:
                    time.sleep(0.01)
                    daemon_lock.acquire()
                    pid_file.write_text(json.dumps(_current_identity(os.getpid())))
                    published.set()
                    release_daemon_lock.wait(timeout=1)
                    daemon_lock.release()

                threading.Thread(target=publish_replacement).start()
                return StubbornChild()

            try:
                with (
                    patch.object(daemon_lifecycle, "READINESS_TIMEOUT_SECONDS", 0.005),
                    patch.object(daemon_lifecycle, "READINESS_POLL_SECONDS", 0.001),
                    patch.object(daemon_lifecycle, "TERMINATE_WAIT_SECONDS", 0.05),
                    patch.object(daemon_lifecycle, "KILL_WAIT_SECONDS", 0.05),
                    patch.object(daemon_lifecycle.subprocess, "Popen", side_effect=spawn),
                ):
                    result = daemon_lifecycle.start_daemon(state_dir)
                self.assertTrue(published.wait(timeout=1))
                self.assertEqual(result.state, "failed")
                self.assertEqual(json.loads(pid_file.read_text()), _current_identity(os.getpid()))
            finally:
                release_daemon_lock.set()

    def test_noisy_pre_ready_stderr_is_drained_without_blocking_readiness(self):
        """More than a pipe buffer of early stderr cannot prevent PID publication."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            ready_to_publish = threading.Event()
            release_daemon_lock = threading.Event()
            writer_done = threading.Event()
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)

            class BlockingStderr:
                def __init__(self):
                    self._output = io.BytesIO(b"x" * (128 * 1024))

                def read(self, size=-1):
                    ready_to_publish.set()
                    return self._output.read(size)

            class Child:
                pid = os.getpid()

                def __init__(self):
                    self.stderr = BlockingStderr()

                @staticmethod
                def poll():
                    return None

            def spawn(*_args, **_kwargs):
                def publish_after_drain() -> None:
                    ready_to_publish.wait(timeout=1)
                    daemon_lock.acquire()
                    pid_file.write_text(json.dumps(_current_identity(os.getpid())))
                    release_daemon_lock.wait(timeout=1)
                    daemon_lock.release()
                    writer_done.set()

                threading.Thread(target=publish_after_drain).start()
                return Child()

            try:
                with patch.object(daemon_lifecycle.subprocess, "Popen", side_effect=spawn):
                    result = daemon_lifecycle.start_daemon(state_dir)
            finally:
                release_daemon_lock.set()
            self.assertTrue(writer_done.wait(timeout=1))

            self.assertEqual(result.state, "started")
            self.assertLessEqual(
                (state_dir / "codex-daemon-startup.log").stat().st_size
                if (state_dir / "codex-daemon-startup.log").exists()
                else 0,
                daemon_lifecycle.STARTUP_LOG_BYTES,
            )

    def test_exited_child_returns_exit_detail_and_writes_bounded_startup_diagnostic(self):
        """Pre-logging startup failures remain diagnosable after the CLI exits."""
        class ExitedChild:
            pid = 424242

            @staticmethod
            def poll():
                return 23

            @staticmethod
            def communicate(timeout=None):
                return b"", b"configuration invalid\n"

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            with patch.object(daemon_lifecycle.subprocess, "Popen", return_value=ExitedChild()):
                result = daemon_lifecycle.start_daemon(state_dir)

            self.assertIn("exit status 23", getattr(result, "detail", ""))
            startup_log = state_dir / "codex-daemon-startup.log"
            self.assertIn("exit status 23", startup_log.read_text())
            self.assertIn("configuration invalid", startup_log.read_text())

    def test_stop_times_out_honestly_when_the_shared_startup_lock_is_held(self):
        """Stop must serialize with startup rather than racing its PID cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            startup_lock = FileLock(str(state_dir / "codex_daemon_startup.lock"), timeout=0)
            startup_lock.acquire()
            try:
                with patch.object(daemon_lifecycle, "STARTUP_LOCK_TIMEOUT_SECONDS", 0.001):
                    result = daemon_lifecycle.stop_daemon(state_dir)
            finally:
                startup_lock.release()

            self.assertEqual(result.state, "failed")
            self.assertIn("startup lock", result.detail)

    def test_stop_preserves_a_live_publisher_that_interleaves_before_cleanup(self):
        """A replacement daemon makes stop fail rather than claim it stopped all daemons."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            original = daemon_lifecycle.DaemonIdentity(111, "old-boot", "old-start")
            winner = daemon_lifecycle.DaemonIdentity(222, "winner-boot", "winner-start")
            pid_file.write_text(json.dumps(original.__dict__))

            def publish_winner(*_args):
                pid_file.write_text(json.dumps(winner.__dict__))

            with (
                patch.object(
                    daemon_lifecycle,
                    "open_validated_daemon_pidfd",
                    return_value=daemon_lifecycle.DaemonPidfd(original.pid, 71),
                ),
                patch.object(
                    daemon_lifecycle,
                    "_record_is_live_daemon",
                    side_effect=lambda _state, record: record == winner,
                ),
                patch.object(daemon_lifecycle, "signal", create=True) as signal_module,
                patch.object(daemon_lifecycle, "select", create=True) as select_module,
                patch.object(daemon_lifecycle.os, "close"),
            ):
                signal_module.pidfd_send_signal.side_effect = publish_winner
                select_module.select.return_value = ([71], [], [])
                result = daemon_lifecycle.stop_daemon(state_dir)

            self.assertEqual(result.state, "failed")
            self.assertIn("replacement", result.detail)
            self.assertEqual(json.loads(pid_file.read_text()), winner.__dict__)

    def test_stop_waits_for_pidfd_exit_before_reporting_success(self):
        """A delivered SIGTERM is not a stopped daemon until its pidfd is readable."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            identity = daemon_lifecycle.DaemonIdentity(111, "boot", "start")
            pid_file.write_text(json.dumps(identity.__dict__))

            with (
                patch.object(
                    daemon_lifecycle,
                    "open_validated_daemon_pidfd",
                    return_value=daemon_lifecycle.DaemonPidfd(identity.pid, 71),
                ),
                patch.object(daemon_lifecycle, "signal", create=True) as signal_module,
                patch.object(daemon_lifecycle, "select", create=True) as select_module,
                patch.object(daemon_lifecycle.os, "close"),
                patch.object(daemon_lifecycle, "_record_is_live_daemon", return_value=False),
            ):
                select_module.select.return_value = ([], [], [])
                result = daemon_lifecycle.stop_daemon(state_dir)

            self.assertEqual(result.state, "failed")
            self.assertIn("did not exit", result.detail)
            self.assertTrue(pid_file.exists())
            signal_module.pidfd_send_signal.assert_called_once()

    def test_stop_removes_its_json_record_only_after_pidfd_reports_exit(self):
        """Successful pidfd stop performs ownership-aware state cleanup after exit."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            identity = daemon_lifecycle.DaemonIdentity(111, "boot", "start")
            pid_file.write_text(json.dumps(identity.__dict__))

            with (
                patch.object(
                    daemon_lifecycle,
                    "open_validated_daemon_pidfd",
                    return_value=daemon_lifecycle.DaemonPidfd(identity.pid, 71),
                ),
                patch.object(daemon_lifecycle, "signal", create=True) as signal_module,
                patch.object(daemon_lifecycle, "select", create=True) as select_module,
                patch.object(daemon_lifecycle.os, "close"),
                patch.object(daemon_lifecycle, "_record_is_live_daemon", return_value=False),
            ):
                select_module.select.return_value = ([71], [], [])
                result = daemon_lifecycle.stop_daemon(state_dir)

            self.assertEqual(result.state, "stopped")
            signal_module.pidfd_send_signal.assert_called_once()
            self.assertFalse(pid_file.exists())


class CodexCliLifecycleContractTests(unittest.TestCase):
    def test_enable_and_disable_serialize_their_complete_mutations(self):
        """Concurrent explicit commands must leave the state of one serial ordering."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            start_entered = threading.Event()
            release_start = threading.Event()
            stop_entered = threading.Event()
            order = []
            results = []

            def delayed_start(_state_dir):
                order.append("start-entered")
                start_entered.set()
                release_start.wait(timeout=1)
                order.append("start-finished")
                return daemon_lifecycle.DaemonStartResult("started", pid=42)

            def record_stop(_state_dir):
                order.append("stop")
                stop_entered.set()
                return daemon_lifecycle.DaemonStopResult("stopped", pid=42)

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(cli, "start_daemon", side_effect=delayed_start),
                patch.object(cli, "stop_daemon", side_effect=record_stop),
            ):
                enable_thread = threading.Thread(
                    target=lambda: results.append(("enable", cli.cmd_enable(object())))
                )
                disable_thread = threading.Thread(
                    target=lambda: results.append(("disable", cli.cmd_disable(object())))
                )
                enable_thread.start()
                self.assertTrue(start_entered.wait(timeout=1))
                disable_thread.start()
                self.assertFalse(stop_entered.wait(timeout=0.1))
                release_start.set()
                enable_thread.join(timeout=1)
                disable_thread.join(timeout=1)

            self.assertEqual(order, ["start-entered", "start-finished", "stop"])
            self.assertEqual(sorted(results), [("disable", 0), ("enable", 0)])
            self.assertFalse(enabled_flag.exists())

    def test_explicit_lifecycle_commands_fail_honestly_on_operation_lock_timeout(self):
        """Enable, disable, start, and stop must not act while another operation owns intent."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            operation_lock = FileLock(str(state_dir / "codex-cli-operation.lock"), timeout=0)
            operation_lock.acquire()
            output = io.StringIO()
            try:
                with (
                    patch.object(cli, "CODEX_STATE_DIR", state_dir),
                    patch.object(cli, "ENABLED_FLAG", state_dir / "codex-enabled"),
                    patch.object(cli, "load_config", return_value=object()),
                    patch.object(cli, "_install_notify_hook", return_value=True),
                    patch.object(
                        cli,
                        "start_daemon",
                        return_value=daemon_lifecycle.DaemonStartResult("already_running", pid=42),
                    ),
                    patch.object(
                        cli,
                        "stop_daemon",
                        return_value=daemon_lifecycle.DaemonStopResult("stopped", pid=42),
                    ),
                    patch.object(cli, "OPERATION_LOCK_TIMEOUT_SECONDS", 0.001, create=True),
                    contextlib.redirect_stdout(output),
                ):
                    results = [
                        cli.cmd_enable(object()),
                        cli.cmd_disable(object()),
                        cli.cmd_start(object()),
                        cli.cmd_stop(object()),
                    ]
            finally:
                operation_lock.release()

            self.assertEqual(results, [1, 1, 1, 1])
            self.assertEqual(output.getvalue().lower().count("operation lock"), 4)

    def test_first_enable_rolls_back_its_owned_daemon_when_flag_publish_fails(self):
        """A daemon launched by this failed enable must not outlive absent enabled state."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            started = daemon_lifecycle.DaemonStartResult("started", pid=42)
            stopped = daemon_lifecycle.DaemonStopResult("stopped", pid=42)
            original_touch = Path.touch

            def reject_flag_touch(path, *args, **kwargs):
                if path == enabled_flag:
                    raise OSError("simulated flag publication failure")
                return original_touch(path, *args, **kwargs)

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(cli, "start_daemon", return_value=started),
                patch.object(cli, "stop_daemon", return_value=stopped) as stop,
                patch.object(Path, "touch", autospec=True, side_effect=reject_flag_touch),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertFalse(enabled_flag.exists())
            stop.assert_called_once_with(state_dir)

    def test_first_enable_does_not_stop_a_converged_daemon_when_flag_publish_fails(self):
        """An enable that converged on another daemon has no destructive rollback authority."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            converged = daemon_lifecycle.DaemonStartResult("already_running", pid=42)
            original_touch = Path.touch

            def reject_flag_touch(path, *args, **kwargs):
                if path == enabled_flag:
                    raise OSError("simulated flag publication failure")
                return original_touch(path, *args, **kwargs)

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(cli, "start_daemon", return_value=converged),
                patch.object(cli, "stop_daemon") as stop,
                patch.object(Path, "touch", autospec=True, side_effect=reject_flag_touch),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertFalse(enabled_flag.exists())
            stop.assert_not_called()

    def test_flag_publication_reports_a_failed_owned_daemon_rollback(self):
        """Failed stop after publication failure must be part of the enable diagnostic."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            original_touch = Path.touch

            def reject_flag_touch(path, *args, **kwargs):
                if path == enabled_flag:
                    raise OSError("simulated flag publication failure")
                return original_touch(path, *args, **kwargs)

            output = io.StringIO()
            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(
                    cli, "start_daemon", return_value=daemon_lifecycle.DaemonStartResult("started", pid=42)
                ),
                patch.object(
                    cli,
                    "stop_daemon",
                    return_value=daemon_lifecycle.DaemonStopResult(
                        "failed", pid=42, detail="daemon did not exit"
                    ),
                ),
                patch.object(Path, "touch", autospec=True, side_effect=reject_flag_touch),
                contextlib.redirect_stdout(output),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertIn("rollback", output.getvalue().lower())
            self.assertIn("daemon did not exit", output.getvalue())

    def test_first_enable_publishes_its_flag_only_after_daemon_readiness(self):
        """A first enable cannot advertise success while startup is still pending."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            child_spawned = threading.Event()
            publish_ready = threading.Event()
            release_daemon_lock = threading.Event()
            writer_done = threading.Event()
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)
            results = []

            class Child:
                pid = os.getpid()

                @staticmethod
                def poll():
                    return None

            def spawn(*_args, **_kwargs):
                child_spawned.set()

                def publish_identity():
                    publish_ready.wait(timeout=1)
                    daemon_lock.acquire()
                    (state_dir / "codex-daemon.pid").write_text(
                        json.dumps(_current_identity(os.getpid()))
                    )
                    release_daemon_lock.wait(timeout=1)
                    daemon_lock.release()
                    writer_done.set()

                threading.Thread(target=publish_identity).start()
                return Child()

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(daemon_lifecycle.subprocess, "Popen", side_effect=spawn),
            ):
                enable_thread = threading.Thread(target=lambda: results.append(cli.cmd_enable(object())))
                enable_thread.start()
                self.assertTrue(child_spawned.wait(timeout=1))
                self.assertFalse(enabled_flag.exists())
                publish_ready.set()
                enable_thread.join(timeout=1)
                release_daemon_lock.set()

            self.assertTrue(writer_done.wait(timeout=1))
            self.assertEqual(results, [0])
            self.assertTrue(enabled_flag.exists())

    def test_failed_reenable_keeps_the_existing_enabled_flag(self):
        """A repair retry must preserve a previously enabled bridge on startup failure."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            enabled_flag.touch()
            failed = daemon_lifecycle.DaemonStartResult("failed", detail="exit status 23")

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(cli, "start_daemon", return_value=failed),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertTrue(enabled_flag.exists())

    def test_enable_failure_returns_nonzero_and_removes_enabled_flag(self):
        """A bridge is not enabled when its daemon could not become ready."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            failed = SimpleNamespace(state="failed", pid=None, detail="exit status 23")

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook"),
                patch.object(cli, "start_daemon", return_value=failed),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertFalse(enabled_flag.exists())

    def test_enable_keeps_its_flag_when_startup_converges_on_another_daemon(self):
        """Converged startup is success for the enabled-state contract."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            converged = daemon_lifecycle.DaemonStartResult("already_running", pid=42)

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=True),
                patch.object(cli, "start_daemon", return_value=converged),
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 0)
            self.assertTrue(enabled_flag.exists())

    def test_start_failure_returns_nonzero_and_surfaces_its_detail(self):
        """A failed child must make the CLI process fail with the observed cause."""
        failed = SimpleNamespace(state="failed", pid=None, detail="exit status 23")
        output = io.StringIO()
        with patch.object(cli, "start_daemon", return_value=failed), contextlib.redirect_stdout(output):
            result = cli.cmd_start(object())

        self.assertEqual(result, 1)
        self.assertIn("exit status 23", output.getvalue())

    def test_stop_and_status_reject_a_malformed_pid_without_crashing(self):
        """Administrative commands must not turn corrupt state into a CLI crash."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            pid_file.write_text("not-a-pid")
            output = io.StringIO()
            errors = []

            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_check_notify_hook"),
                contextlib.redirect_stdout(output),
            ):
                try:
                    stop_result = cli.cmd_stop(object())
                    status_result = cli.cmd_status(object())
                except ValueError as error:
                    errors.append(error)
                    stop_result = status_result = None

            self.assertEqual(errors, [])
            self.assertEqual(stop_result, 0)
            self.assertEqual(status_result, 0)
            self.assertIn("not running", output.getvalue())

    def test_stop_does_not_signal_a_reused_identity_record(self):
        """Stop may only target a currently validated daemon identity."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            stale = _current_identity(os.getpid())
            stale["boot_id"] = "a-different-boot"
            pid_file.write_text(json.dumps(stale))
            output = io.StringIO()
            errors = []

            with patch.object(cli, "CODEX_STATE_DIR", state_dir), contextlib.redirect_stdout(output):
                try:
                    result = cli.cmd_stop(object())
                except ValueError as error:
                    errors.append(error)
                    result = None

            self.assertEqual(errors, [])
            self.assertEqual(result, 0)
            self.assertFalse(pid_file.exists())
            self.assertIn("not running", output.getvalue())

    def test_stop_fails_closed_when_pidfds_are_unavailable(self):
        """Stop must never regress to PID-only signalling on old runtimes."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            pid_file.write_text(json.dumps(_current_identity(os.getpid())))
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)
            daemon_lock.acquire()
            output = io.StringIO()
            try:
                with (
                    patch.object(cli, "CODEX_STATE_DIR", state_dir),
                    patch.object(cli.os, "pidfd_open", side_effect=OSError("unsupported"), create=True),
                    patch.object(cli.os, "kill") as kill,
                    contextlib.redirect_stdout(output),
                ):
                    result = cli.cmd_stop(object())
            finally:
                daemon_lock.release()

            self.assertEqual(result, 1)
            kill.assert_called_once_with(os.getpid(), 0)
            self.assertTrue(pid_file.exists())
            self.assertIn("pidfd", output.getvalue().lower())

    def test_pidfd_validation_rejects_a_process_replaced_after_open(self):
        """A pidfd opened for an old process must not signal a replacement PID."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            pid_file.write_text(json.dumps(_current_identity(os.getpid())))
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)
            daemon_lock.acquire()
            opened = []
            try:
                with (
                    patch.object(daemon_lifecycle.os, "pidfd_open", return_value=71, create=True),
                    patch.object(
                        daemon_lifecycle,
                        "_current_identity",
                        return_value=daemon_lifecycle.DaemonIdentity(
                            os.getpid(), "different-boot", "different-start"
                        ),
                    ),
                    patch.object(daemon_lifecycle.os, "close", side_effect=opened.append),
                ):
                    handle = daemon_lifecycle.open_validated_daemon_pidfd(state_dir)
            finally:
                daemon_lock.release()

            self.assertIsNone(handle)
            self.assertIn(71, opened)

    def test_stop_rejects_a_legacy_pid_even_when_it_looks_like_the_daemon(self):
        """Plaintext state cannot bind destructive authority across PID reuse."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            pid_file = state_dir / "codex-daemon.pid"
            pid_file.write_text(str(os.getpid()))
            daemon_lock = FileLock(str(state_dir / "codex-daemon.lock"), timeout=0)
            daemon_lock.acquire()
            try:
                with (
                    patch.object(cli, "CODEX_STATE_DIR", state_dir),
                    patch.object(cli.os, "kill") as kill,
                    patch.object(daemon_lifecycle, "_is_codex_module_process", return_value=True),
                    patch.object(cli.os, "pidfd_open", return_value=71, create=True),
                    patch.object(daemon_lifecycle.signal, "pidfd_send_signal") as send_signal,
                ):
                    result = cli.cmd_stop(object())
            finally:
                daemon_lock.release()

            self.assertEqual(result, 1)
            kill.assert_not_called()
            send_signal.assert_not_called()
            self.assertTrue(pid_file.exists())

    def test_enable_rolls_back_when_notify_hook_installation_fails(self):
        """A failed config update cannot leave Matrix marked enabled."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(cli, "load_config", return_value=object()),
                patch.object(cli, "_install_notify_hook", return_value=False),
                patch.object(cli, "start_daemon") as start,
            ):
                result = cli.cmd_enable(object())

            self.assertEqual(result, 1)
            self.assertFalse(enabled_flag.exists())
            start.assert_not_called()

    def test_disable_keeps_enabled_flag_when_stop_fails(self):
        """Disable reports a failed stop instead of claiming the bridge is off."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            enabled_flag.parent.mkdir(exist_ok=True)
            enabled_flag.touch()
            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(
                    cli,
                    "stop_daemon",
                    return_value=daemon_lifecycle.DaemonStopResult(
                        "failed", detail="daemon did not exit"
                    ),
                ),
            ):
                result = cli.cmd_disable(object())

            self.assertEqual(result, 1)
            self.assertTrue(enabled_flag.exists())

    def test_disable_recovers_when_restore_fails_but_second_safe_stop_succeeds(self):
        """A failed enabled-flag restore becomes disabled only after a second safe stop proves exit."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            enabled_flag.touch()
            original_touch = Path.touch

            def reject_restore(path, *args, **kwargs):
                if path == enabled_flag:
                    raise OSError("simulated restore failure")
                return original_touch(path, *args, **kwargs)

            first_failure = daemon_lifecycle.DaemonStopResult(
                "failed", pid=42, detail="first stop timed out"
            )
            output = io.StringIO()
            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(
                    cli,
                    "stop_daemon",
                    side_effect=[
                        first_failure,
                        daemon_lifecycle.DaemonStopResult("stopped", pid=42),
                    ],
                ) as stop,
                patch.object(Path, "touch", autospec=True, side_effect=reject_restore),
                contextlib.redirect_stdout(output),
            ):
                result = cli.cmd_disable(object())

            self.assertEqual(result, 0)
            self.assertFalse(enabled_flag.exists())
            self.assertEqual(stop.call_count, 2)
            self.assertIn("restoration failed", output.getvalue().lower())
            self.assertIn("disabled", output.getvalue().lower())

    def test_disable_reports_both_restore_and_second_stop_failures(self):
        """An unproven live daemon must not be masked when its prior flag cannot be restored."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            enabled_flag.touch()
            original_touch = Path.touch

            def reject_restore(path, *args, **kwargs):
                if path == enabled_flag:
                    raise OSError("simulated restore failure")
                return original_touch(path, *args, **kwargs)

            output = io.StringIO()
            with (
                patch.object(cli, "CODEX_STATE_DIR", state_dir),
                patch.object(cli, "ENABLED_FLAG", enabled_flag),
                patch.object(
                    cli,
                    "stop_daemon",
                    side_effect=[
                        daemon_lifecycle.DaemonStopResult(
                            "failed", pid=42, detail="first stop timed out"
                        ),
                        daemon_lifecycle.DaemonStopResult(
                            "failed", pid=42, detail="second stop timed out"
                        ),
                    ],
                ) as stop,
                patch.object(Path, "touch", autospec=True, side_effect=reject_restore),
                contextlib.redirect_stdout(output),
            ):
                result = cli.cmd_disable(object())

            self.assertEqual(result, 1)
            self.assertFalse(enabled_flag.exists())
            self.assertEqual(stop.call_count, 2)
            self.assertIn("restore failure", output.getvalue().lower())
            self.assertIn("second stop timed out", output.getvalue())

    def test_disable_removes_flag_before_a_waiting_notify_can_start(self):
        """A notify that passed its early flag check must re-check it under the startup lock."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            enabled_flag = state_dir / "codex-enabled"
            enabled_flag.touch()
            startup_lock = FileLock(str(state_dir / daemon_lifecycle.STARTUP_LOCK_NAME), timeout=0)
            startup_lock.acquire()
            notify_called = threading.Event()
            notify_result = []
            lock_released = False

            def notify_start() -> None:
                notify_called.set()
                notify_result.append(
                    daemon_lifecycle.start_daemon(
                        state_dir, required_enabled_flag=enabled_flag
                    )
                )

            def stop_after_disabling(_state_dir):
                nonlocal lock_released
                self.assertFalse(enabled_flag.exists())
                startup_lock.release()
                lock_released = True
                notify_thread.join(timeout=1)
                return daemon_lifecycle.DaemonStopResult("stopped", pid=42)

            notify_thread = threading.Thread(target=notify_start)
            notify_thread.start()
            self.assertTrue(notify_called.wait(timeout=1))
            try:
                with (
                    patch.object(cli, "CODEX_STATE_DIR", state_dir),
                    patch.object(cli, "ENABLED_FLAG", enabled_flag),
                    patch.object(cli, "stop_daemon", side_effect=stop_after_disabling),
                    patch.object(daemon_lifecycle.subprocess, "Popen") as popen,
                ):
                    result = cli.cmd_disable(object())
            finally:
                if not lock_released:
                    startup_lock.release()
                notify_thread.join(timeout=1)

            self.assertEqual(result, 0)
            self.assertEqual([outcome.state for outcome in notify_result], ["disabled"])
            popen.assert_not_called()
            self.assertFalse(enabled_flag.exists())

    def test_notify_logs_the_concrete_start_failure(self):
        """Notify auto-start retains enough detail to diagnose a failed child."""
        failed = SimpleNamespace(state="failed", pid=None, detail="exit status 23")
        with patch.object(notify_handler, "start_daemon", return_value=failed) as start:
            with self.assertLogs("codex_matrix.notify_handler", level="ERROR") as logs:
                notify_handler._ensure_daemon_running()

        self.assertIn("exit status 23", "\n".join(logs.output))
        start.assert_called_once_with(
            notify_handler.STATE_DIR, required_enabled_flag=notify_handler.ENABLED_FLAG
        )

    def test_notify_does_not_log_a_failure_after_startup_converges(self):
        """Notify auto-start treats another ready daemon as the desired result."""
        converged = daemon_lifecycle.DaemonStartResult("already_running", pid=42)
        with patch.object(notify_handler, "start_daemon", return_value=converged) as start:
            with self.assertNoLogs("codex_matrix.notify_handler", level="ERROR"):
                notify_handler._ensure_daemon_running()

        start.assert_called_once_with(
            notify_handler.STATE_DIR, required_enabled_flag=notify_handler.ENABLED_FLAG
        )


class CodexDaemonShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_prestart_shutdown_does_not_enter_runtime_contexts(self):
        """A shutdown requested before start performs no bridge, discovery, or watcher work."""
        daemon_instance = daemon.CodexDaemon.__new__(daemon.CodexDaemon)
        daemon_instance.running = False
        daemon_instance._reset_runtime_state()

        class Context:
            def __init__(self):
                self.entries = 0

            async def __aenter__(self):
                self.entries += 1
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        bridge = Context()
        poll = Context()
        discover = AsyncMock()
        matrix_loop = AsyncMock()
        signal_loop = AsyncMock()
        cleanup_loop = AsyncMock()
        daemon_instance.bridge = bridge
        daemon_instance.poll_client = poll
        daemon_instance._discover_sessions = discover
        daemon_instance._matrix_poll_loop = matrix_loop
        daemon_instance._signal_watch_loop = signal_loop
        daemon_instance._session_cleanup_loop = cleanup_loop

        with patch.object(daemon, "SessionWatcher") as watcher:
            await daemon_instance.start()

        self.assertEqual(bridge.entries, 0)
        self.assertEqual(poll.entries, 0)
        discover.assert_not_awaited()
        watcher.assert_not_called()
        matrix_loop.assert_not_called()
        signal_loop.assert_not_called()
        cleanup_loop.assert_not_called()
        self.assertEqual(daemon_instance._runtime_tasks, set())
        self.assertIsNone(daemon_instance._start_task)

    async def test_request_shutdown_cancels_blocked_discovery_and_exits_contexts(self):
        """Shutdown must interrupt discovery before runtime loop tasks even exist."""
        daemon_instance = daemon.CodexDaemon.__new__(daemon.CodexDaemon)
        daemon_instance.running = True
        daemon_instance._reset_runtime_state()
        discovery_started = asyncio.Event()
        never = asyncio.Event()
        bridge_exited = asyncio.Event()
        poll_exited = asyncio.Event()

        class Context:
            def __init__(self, exited):
                self.exited = exited

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                self.exited.set()

        class Watcher:
            def __init__(self):
                self.stopped = False

            def start(self, _loop):
                return None

            def stop(self):
                self.stopped = True

        async def blocked_discovery():
            discovery_started.set()
            await never.wait()

        watcher = Watcher()
        daemon_instance.bridge = Context(bridge_exited)
        daemon_instance.poll_client = Context(poll_exited)
        daemon_instance._discover_sessions = blocked_discovery

        with patch.object(daemon, "SessionWatcher", return_value=watcher):
            start_task = asyncio.create_task(daemon_instance.start())
            await asyncio.wait_for(discovery_started.wait(), timeout=0.5)
            daemon_instance.request_shutdown()
            await asyncio.wait_for(start_task, timeout=0.5)

        self.assertTrue(watcher.stopped)
        self.assertTrue(bridge_exited.is_set())
        self.assertTrue(poll_exited.is_set())
        self.assertEqual(daemon_instance._runtime_tasks, set())
        self.assertIsNone(daemon_instance._start_task)

    async def test_external_start_cancellation_still_propagates(self):
        """Only deliberate shutdown suppresses cancellation from the start task."""
        daemon_instance = daemon.CodexDaemon.__new__(daemon.CodexDaemon)
        daemon_instance.running = True
        daemon_instance._reset_runtime_state()
        discovery_started = asyncio.Event()
        never = asyncio.Event()

        class Context:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        class Watcher:
            def start(self, _loop):
                return None

            def stop(self):
                return None

        async def blocked_discovery():
            discovery_started.set()
            await never.wait()

        daemon_instance.bridge = Context()
        daemon_instance.poll_client = Context()
        daemon_instance._discover_sessions = blocked_discovery

        with patch.object(daemon, "SessionWatcher", return_value=Watcher()):
            start_task = asyncio.create_task(daemon_instance.start())
            await asyncio.wait_for(discovery_started.wait(), timeout=0.5)
            start_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await start_task

        self.assertIsNone(daemon_instance._start_task)

    async def test_request_shutdown_cancels_blocked_runtime_loops_and_runs_cleanup(self):
        """SIGTERM need not wait for Matrix long poll or cleanup sleep to finish."""
        daemon_instance = daemon.CodexDaemon.__new__(daemon.CodexDaemon)
        daemon_instance.running = True
        daemon_instance._reset_runtime_state()
        entered = [asyncio.Event() for _ in range(3)]
        never = asyncio.Event()
        bridge_exited = asyncio.Event()
        poll_exited = asyncio.Event()

        class Context:
            def __init__(self, exited):
                self.exited = exited

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                self.exited.set()

        class Watcher:
            def __init__(self):
                self.stopped = False

            def start(self, _loop):
                return None

            def stop(self):
                self.stopped = True

        async def blocked(index):
            entered[index].set()
            await never.wait()

        watcher = Watcher()
        daemon_instance.bridge = Context(bridge_exited)
        daemon_instance.poll_client = Context(poll_exited)
        daemon_instance._discover_sessions = AsyncMock()
        daemon_instance._matrix_poll_loop = lambda: blocked(0)
        daemon_instance._signal_watch_loop = lambda: blocked(1)
        daemon_instance._session_cleanup_loop = lambda: blocked(2)

        with patch.object(daemon, "SessionWatcher", return_value=watcher):
            start_task = asyncio.create_task(daemon_instance.start())
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in entered)), timeout=0.5
            )
            daemon_instance.request_shutdown()
            await asyncio.wait_for(start_task, timeout=0.5)

        self.assertFalse(daemon_instance.running)
        self.assertTrue(watcher.stopped)
        self.assertTrue(bridge_exited.is_set())
        self.assertTrue(poll_exited.is_set())
        self.assertEqual(daemon_instance._runtime_tasks, set())


if __name__ == "__main__":
    unittest.main()
