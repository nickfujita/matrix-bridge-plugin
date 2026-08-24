"""Shared identity and startup protocol for the Codex Matrix daemon."""

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout


PID_FILE_NAME = "codex-daemon.pid"
DAEMON_LOCK_NAME = "codex-daemon.lock"
STARTUP_LOCK_NAME = "codex_daemon_startup.lock"
STARTUP_LOG_NAME = "codex-daemon-startup.log"
STARTUP_LOCK_TIMEOUT_SECONDS = 5
READINESS_TIMEOUT_SECONDS = 5
READINESS_POLL_SECONDS = 0.05
TERMINATE_WAIT_SECONDS = 1
KILL_WAIT_SECONDS = 1
STOP_WAIT_SECONDS = 2
STARTUP_LOG_BYTES = 16 * 1024


@dataclass(frozen=True)
class DaemonIdentity:
    """PID identity that cannot survive a reboot or PID reuse."""

    pid: int
    boot_id: str | None
    start_time: str | None


@dataclass(frozen=True)
class DaemonStartResult:
    """The observed daemon state after one serialized startup attempt."""

    state: str
    pid: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class DaemonPidfd:
    """A process handle that cannot be redirected by numerical PID reuse."""

    pid: int
    fd: int


@dataclass(frozen=True)
class DaemonStopResult:
    """The observed daemon state after one serialized stop attempt."""

    state: str
    pid: int | None = None
    detail: str = ""


def _current_identity(pid: int) -> DaemonIdentity | None:
    """Read a Linux PID's boot generation and process start tick."""
    if pid <= 0:
        return None

    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text()
        start_time = stat[stat.rfind(")") + 2 :].split()[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (IndexError, OSError):
        return None

    return DaemonIdentity(pid=pid, boot_id=boot_id, start_time=start_time)


def _read_record(pid_file: Path) -> DaemonIdentity | None:
    """Read a current JSON record or legacy plaintext PID without trusting it."""
    try:
        raw = pid_file.read_text().strip()
    except OSError:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        pid = data.get("pid")
        boot_id = data.get("boot_id")
        start_time = data.get("start_time")
        if (
            isinstance(pid, int)
            and not isinstance(pid, bool)
            and pid > 0
            and isinstance(boot_id, str)
            and boot_id
            and isinstance(start_time, str)
            and start_time
        ):
            return DaemonIdentity(pid=pid, boot_id=boot_id, start_time=start_time)
        return None

    try:
        pid = int(raw)
    except ValueError:
        return None
    return DaemonIdentity(pid=pid, boot_id=None, start_time=None) if pid > 0 else None


def _daemon_lock_is_held(state_dir: Path) -> bool:
    """Return whether the daemon's single-instance lock is presently owned."""
    lock = FileLock(str(state_dir / DAEMON_LOCK_NAME), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return True
    else:
        lock.release()
        return False


def _is_codex_module_process(pid: int) -> bool:
    """Recognise the legacy daemon command without treating PID liveness as identity."""
    try:
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False

    decoded = [argument.decode("utf-8", errors="replace") for argument in argv if argument]
    return (
        bool(decoded)
        and Path(decoded[0]).name.startswith("python")
        and any(
            decoded[index] == "-m" and decoded[index + 1] == "codex_matrix"
            for index in range(len(decoded) - 1)
        )
    )


def _record_matches_current(record: DaemonIdentity, current: DaemonIdentity | None) -> bool:
    if current is None:
        return False
    if record.boot_id is None and record.start_time is None:
        return _is_codex_module_process(record.pid)
    return current.boot_id == record.boot_id and current.start_time == record.start_time


def _validated_record(state_dir: Path) -> DaemonIdentity | None:
    """Return a live daemon record only after lock and process identity agree."""
    record = _read_record(state_dir / PID_FILE_NAME)
    return record if record is not None and _record_is_live_daemon(state_dir, record) else None


def _record_is_live_daemon(state_dir: Path, record: DaemonIdentity) -> bool:
    """Check a record directly, including while cleanup has moved its file aside."""
    return _daemon_lock_is_held(state_dir) and _record_matches_current(
        record, _current_identity(record.pid)
    )


def running_daemon_pid(state_dir: Path) -> int | None:
    """Return a validated running daemon PID, never PID liveness alone.

    Current records bind PID to both the boot id and the process's start tick.
    A legacy plaintext PID also needs the daemon lock and a positive
    ``python -m codex_matrix`` command identity, so an actually running old
    release survives an upgrade without accepting a post-reboot PID reuse.
    """
    record = _validated_record(state_dir)
    return record.pid if record is not None else None


def open_validated_daemon_pidfd(
    state_dir: Path, record: DaemonIdentity | None = None
) -> DaemonPidfd | None:
    """Open a pidfd and validate the record against the process it refers to.

    Reading /proc *after* opening the pidfd matters: if the original process
    exits and its numeric PID is reused before the read, its identity will no
    longer match the record, while signalling the pidfd can never target the
    replacement. Platforms without Linux pidfds fail closed.
    """
    record = record or _read_record(state_dir / PID_FILE_NAME)
    pidfd_open = getattr(os, "pidfd_open", None)
    if (
        record is None
        or record.boot_id is None
        or record.start_time is None
        or pidfd_open is None
        or not _daemon_lock_is_held(state_dir)
    ):
        return None

    try:
        fd = pidfd_open(record.pid)
    except OSError:
        return None

    if _record_matches_current(record, _current_identity(record.pid)):
        return DaemonPidfd(record.pid, fd)

    try:
        os.close(fd)
    except OSError:
        pass
    return None


def _stop_failure(detail: str, pid: int | None = None) -> DaemonStopResult:
    return DaemonStopResult("failed", pid=pid, detail=detail)


def stop_daemon(state_dir: Path) -> DaemonStopResult:
    """Stop a JSON-identified daemon under the same lock used by startup.

    The startup lock spans PID-file validation, pidfd creation, signalling,
    exit observation, and cleanup. A plaintext PID remains observable for
    upgrade compatibility but cannot grant destructive authority because it
    lacks the generation data needed to bind the pidfd operation to its record.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    startup_lock = FileLock(
        str(state_dir / STARTUP_LOCK_NAME), timeout=STARTUP_LOCK_TIMEOUT_SECONDS
    )
    try:
        startup_lock.acquire()
    except Timeout:
        return _stop_failure("timed out waiting for daemon startup lock")

    try:
        record = _read_record(state_dir / PID_FILE_NAME)
        if record is None:
            remove_stale_or_owned_pid_record(state_dir)
            return DaemonStopResult("not_running")
        if record.boot_id is None or record.start_time is None:
            return _stop_failure(
                "refusing to stop legacy plaintext PID record; restart the daemon to migrate it",
                record.pid,
            )

        target = open_validated_daemon_pidfd(state_dir, record)
        if target is None:
            if _record_is_live_daemon(state_dir, record):
                return _stop_failure(
                    "Linux pidfd validation is unavailable for the recorded daemon", record.pid
                )
            remove_stale_or_owned_pid_record(state_dir)
            return DaemonStopResult("not_running")

        try:
            pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
            if pidfd_send_signal is None:
                return _stop_failure("Linux pidfd signalling is unavailable", target.pid)
            pidfd_send_signal(target.fd, signal.SIGTERM)
            try:
                readable, _writable, _exceptional = select.select(
                    [target.fd], [], [], STOP_WAIT_SECONDS
                )
            except OSError as error:
                return _stop_failure(f"failed while waiting for daemon exit: {error}", target.pid)
            if not readable:
                return _stop_failure(
                    f"daemon did not exit within {STOP_WAIT_SECONDS} seconds after SIGTERM", target.pid
                )

            remove_stale_or_owned_pid_record(state_dir, record)
            replacement_pid = running_daemon_pid(state_dir)
            if replacement_pid is not None:
                return _stop_failure(
                    f"daemon replacement with live PID {replacement_pid} while stopping",
                    replacement_pid,
                )
            return DaemonStopResult("stopped", target.pid)
        except OSError as error:
            return _stop_failure(f"failed to stop daemon: {error}", target.pid)
        finally:
            try:
                os.close(target.fd)
            except OSError:
                pass
    finally:
        startup_lock.release()


def remove_stale_or_owned_pid_record(
    state_dir: Path, owned_identity: DaemonIdentity | None = None
) -> None:
    """Remove only malformed/stale state or a record published by this child.

    Startup can lose a race to a separately started daemon. Its identity file
    is authoritative once it is live and holds the daemon lock, even if the
    child this attempt launched must be killed. This deliberately does not
    delete a different valid daemon's record.
    """
    pid_file = state_dir / PID_FILE_NAME

    def removable(record: DaemonIdentity | None) -> bool:
        return (
            record is None
            or record == owned_identity
            or not _record_is_live_daemon(state_dir, record)
        )

    record = _read_record(pid_file)
    if not removable(record):
        return

    # ``unlink`` after a separate read can erase a record another daemon
    # publishes in the gap. Move the pathname aside instead. A publisher that
    # races after this rename writes a new pathname, which we never touch.
    quarantine = state_dir / (
        f".{PID_FILE_NAME}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.cleanup"
    )
    try:
        os.replace(pid_file, quarantine)
    except FileNotFoundError:
        return
    except OSError:
        return

    moved_record = _read_record(quarantine)
    if not removable(moved_record):
        # The replacement won the race. Restore it only when no newer record
        # has appeared at the public path; hard-link creation is no-clobber.
        try:
            os.link(quarantine, pid_file)
        except FileExistsError:
            pass
        except OSError:
            # Leaving the quarantined record is safer than overwriting a
            # concurrent publisher on a filesystem without hard links.
            return
    try:
        quarantine.unlink(missing_ok=True)
    except OSError:
        pass


def write_daemon_identity(state_dir: Path) -> DaemonIdentity:
    """Atomically publish this daemon's PID identity after it owns its lock."""
    identity = _current_identity(os.getpid())
    if identity is None or identity.boot_id is None or identity.start_time is None:
        raise RuntimeError("unable to determine daemon process identity")

    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / PID_FILE_NAME
    temporary = state_dir / f".{PID_FILE_NAME}.{identity.pid}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "pid": identity.pid,
                "boot_id": identity.boot_id,
                "start_time": identity.start_time,
            }
        )
        + "\n"
    )
    os.replace(temporary, pid_file)
    return identity


def _record_startup_failure(state_dir: Path, detail: str, stderr: bytes = b"") -> None:
    """Persist one bounded diagnostic for failures before daemon logging exists."""
    output = stderr.decode("utf-8", errors="replace").strip()
    message = f"{detail}\n"
    if output:
        message += f"{output}\n"
    (state_dir / STARTUP_LOG_NAME).write_bytes(message.encode("utf-8")[-STARTUP_LOG_BYTES:])


def _child_stderr(process: subprocess.Popen[bytes]) -> bytes:
    try:
        _stdout, stderr = process.communicate(timeout=KILL_WAIT_SECONDS)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        return b""
    return stderr if isinstance(stderr, bytes) else b""


class _StderrCapture:
    """Drain a launched daemon's stderr without retaining unbounded output."""

    def __init__(self, process: subprocess.Popen[bytes]):
        self._process = process
        self._stream = getattr(process, "stderr", None)
        self._output = bytearray()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        if self._stream is not None and hasattr(self._stream, "read"):
            self._thread = threading.Thread(target=self._drain, daemon=True)
            self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(4096)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                with self._lock:
                    self._output.extend(chunk)
                    del self._output[:-STARTUP_LOG_BYTES]
        except (OSError, ValueError):
            return

    def output(self) -> bytes:
        if self._thread is None:
            return _child_stderr(self._process)
        self._thread.join(timeout=KILL_WAIT_SECONDS)
        with self._lock:
            return bytes(self._output)


def _stop_owned_child(
    process: subprocess.Popen[bytes], capture: _StderrCapture
) -> tuple[int | None, bytes]:
    """Terminate, then kill, a child launched by this startup attempt."""
    try:
        process.terminate()
    except (AttributeError, OSError):
        pass

    try:
        process.wait(timeout=TERMINATE_WAIT_SECONDS)
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except (AttributeError, OSError):
            pass
        try:
            process.wait(timeout=KILL_WAIT_SECONDS)
        except (AttributeError, OSError, subprocess.TimeoutExpired):
            pass

    return process.poll(), capture.output()


def _failure(state_dir: Path, detail: str, stderr: bytes = b"") -> DaemonStartResult:
    _record_startup_failure(state_dir, detail, stderr)
    return DaemonStartResult("failed", detail=detail)


def start_daemon(
    state_dir: Path, *, required_enabled_flag: Path | None = None
) -> DaemonStartResult:
    """Start one daemon and wait until that child publishes its identity.

    Both the explicit CLI and completion-notify handler hold the same lock
    through the entire readiness/shutdown window, preventing competing launch
    attempts from escaping before the daemon writes its identity record.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_file = state_dir / PID_FILE_NAME
    startup_lock = FileLock(
        str(state_dir / STARTUP_LOCK_NAME), timeout=STARTUP_LOCK_TIMEOUT_SECONDS
    )
    try:
        startup_lock.acquire()
    except Timeout:
        return _failure(state_dir, "timed out waiting for daemon startup lock")

    try:
        # Notify may have observed the flag before disable acquired the stop
        # lock. Re-check it only after owning this same lock, so a completion
        # cannot revive the daemon after disable removes the flag.
        if required_enabled_flag is not None and not required_enabled_flag.exists():
            return DaemonStartResult("disabled")

        pid = running_daemon_pid(state_dir)
        if pid is not None:
            return DaemonStartResult("already_running", pid)

        # Stale, malformed, and legacy-after-reboot records cannot identify a
        # daemon. Remove them before launching so a failed child leaves no lie.
        remove_stale_or_owned_pid_record(state_dir)
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "codex_matrix"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as error:
            return _failure(state_dir, f"failed to launch daemon: {error}")
        capture = _StderrCapture(process)
        child_identity = _current_identity(process.pid)

        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            pid = running_daemon_pid(state_dir)
            if pid == process.pid:
                return DaemonStartResult("started", pid)
            if pid is not None:
                _stop_owned_child(process, capture)
                remove_stale_or_owned_pid_record(state_dir, child_identity)
                # A concurrent caller completed startup first. Its validated
                # record is the desired converged state, not a failure for this
                # launcher (and must stay visible to enable/notify callers).
                return DaemonStartResult("already_running", pid)
            exit_status = process.poll()
            if exit_status is not None:
                remove_stale_or_owned_pid_record(state_dir, child_identity)
                return _failure(
                    state_dir,
                    f"daemon exited with exit status {exit_status}",
                    capture.output(),
                )
            time.sleep(READINESS_POLL_SECONDS)

        exit_status, stderr = _stop_owned_child(process, capture)
        remove_stale_or_owned_pid_record(state_dir, child_identity)
        return _failure(
            state_dir,
            f"daemon did not become ready before timeout (child exit status {exit_status})",
            stderr,
        )
    finally:
        startup_lock.release()
