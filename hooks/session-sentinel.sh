#!/usr/bin/env bash
# SessionStart sentinel — teaches the agent the two rules that only the phone
# bridge knows about:
#
#   1. a turn may be read aloud on a phone, so switch to the TTS-safe skill as
#      soon as the user hints they are mobile, and
#   2. a nested agent CLI started in the bridge-owned tmux pane silently steals
#      that pane's session mapping (see SessionMap.register), which breaks
#      inbound phone → terminal routing while outbound still looks healthy.
#
# Deliberately dependency-free (no uv, no Python): it must stay a few
# milliseconds and must not fail the session if the workspace venv is broken.
# Runs under both Claude Code and Codex — both read hooks/hooks.json and both
# accept hookSpecificOutput.additionalContext on SessionStart.
set -euo pipefail

state_dir="${HOME}/.ccmatrix"

# Only spend context when a bridge is actually wired up on this machine.
# ~/.ccmatrix/enabled is the Claude Code flag, codex-enabled the Codex one.
if [[ ! -f "${state_dir}/enabled" && ! -f "${state_dir}/codex-enabled" ]]; then
  echo '{}'
  exit 0
fi

cat <<'SENTINEL'
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "Messages may arrive via the Matrix phone bridge. On any hint the user is mobile — dictation artifacts/typos, phrases like 'heading out' or 'going mobile', or bridge-originated messages — invoke the go-mobile skill immediately (if installed). Never launch a nested claude/codex/live-agent CLI session in the tmux pane owned by the bridge; use a separate tmux session or Docker."
  }
}
SENTINEL
