"""Unit tests using fake /proc entries; no Matrix or tmux services required."""

from matrix_bridge.tmux import pane_for_open_file


def make_holder(tmp_path, command):
    target = tmp_path / "rollout-thread.jsonl"
    target.write_text("")
    proc = tmp_path / "proc"
    process = proc / "42"
    (process / "fd").mkdir(parents=True)
    (process / "fd" / "3").symlink_to(target)
    (process / "environ").write_bytes(b"TMUX_PANE=%0\0")
    (process / "cmdline").write_bytes(command)
    return target, proc, process


def test_direct_cli_holder_identifies_its_pane(tmp_path):
    target, proc, _ = make_holder(tmp_path, b"codex\0resume\0")
    assert pane_for_open_file(target, proc_root=proc) == "%0"


def test_shared_app_server_cannot_claim_its_launch_pane(tmp_path):
    target, proc, _ = make_holder(
        tmp_path, b"/opt/codex\0app-server\0--managed-daemon\0"
    )
    assert pane_for_open_file(target, proc_root=proc) is None


def test_unknown_holder_command_cannot_claim_a_pane(tmp_path):
    target, proc, process = make_holder(tmp_path, b"codex\0")
    (process / "cmdline").rename(process / "cmdline-unavailable")
    assert pane_for_open_file(target, proc_root=proc) is None
