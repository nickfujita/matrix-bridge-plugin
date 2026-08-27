#!/usr/bin/env bash
# SessionStart sentinel — teaches the agent the two rules that only the phone
# bridge knows about:
#
#   1. a turn may be read aloud on a phone, so switch to the TTS-safe skill for
#      replies that go back out over the bridge, and
#   2. a nested agent CLI started in the bridge-owned tmux pane silently steals
#      that pane's session mapping (see SessionMap.register), which breaks
#      inbound phone → terminal routing while outbound still looks healthy.
#
# Deliberately dependency-free (no uv, no Python): it must stay a few
# milliseconds and must not fail the session if the workspace venv is broken.
# Runs under both Claude Code and Codex — both read hooks/hooks.json and both
# accept hookSpecificOutput.additionalContext on SessionStart.
#
# Keep the payload terse. Claude Code absorbs additionalContext silently, but
# Codex *renders it to the user* as a "hook context:" block on the first turn of
# every session: codex-rs/hooks/src/events/common.rs pushes the same string into
# both the model's context and the user-visible entry list, and SessionStart
# discards `suppressOutput` (`let _ = parsed.universal.suppress_output;` in
# codex-rs/hooks/src/events/session_start.rs). There is no quiet channel, so the
# only lever on that wall of text is length. `suppressOutput` is still declared
# below: Claude Code honours it, and Codex silences the echo for free if it ever
# stops ignoring the field.
set -euo pipefail

state_dir="${HOME}/.ccmatrix"

# Only spend context when a bridge is actually wired up on this machine.
# ~/.ccmatrix/enabled is the Claude Code flag, codex-enabled the Codex one.
if [[ ! -f "${state_dir}/enabled" && ! -f "${state_dir}/codex-enabled" ]]; then
  echo '{}'
  exit 0
fi

# A session another agent's flow spawned is never mirrored to the phone, so
# these rules are context it can only be misled by: there is no human on the
# other end to go mobile for. Same truthiness as the Python side
# (matrix_bridge.config.is_suppressed_session).
case "$(printf '%s' "${CCMATRIX_SUPPRESS_SESSION:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in
  ''|0|false|no|off) ;;
  *)
    echo '{}'
    exit 0
    ;;
esac

cat <<'SENTINEL'
{
  "suppressOutput": true,
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Matrix phone bridge active. Use go-mobile only for bridge-delivered replies or an explicit /go-mobile, not for dictation typos. Never start a nested claude/codex CLI in the bridge's tmux pane — it steals the pane's session mapping and breaks inbound routing."
  }
}
SENTINEL
