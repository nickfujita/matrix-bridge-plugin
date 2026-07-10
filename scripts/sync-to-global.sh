#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

dry_run="false"
restart_daemon="true"
refresh_rooms="true"

usage() {
  cat <<USAGE
Usage: scripts/sync-to-global.sh [dry|--dry-run] [--no-restart] [--no-refresh-rooms]

Mirror this repo into the global Claude plugin cache and refresh Codex wiring.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    dry|--dry-run)
      dry_run="true"
      shift
      ;;
    --no-restart)
      restart_daemon="false"
      shift
      ;;
    --no-refresh-rooms)
      refresh_rooms="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    "")
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"
PLUGIN_ID="claude-code-matrix@claude-code-matrix"
CACHE_ROOT="$CLAUDE_HOME/plugins/cache/claude-code-matrix/claude-code-matrix"
INSTALL_FILE="$CLAUDE_HOME/plugins/installed_plugins.json"

VERSION="$(
  python3 - "$REPO_ROOT/.claude-plugin/plugin.json" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1]))["version"])
PY
)"
CACHE_PATH="$CACHE_ROOT/$VERSION"
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

echo "Version: $VERSION"
echo "Cache path: $CACHE_PATH"

copy_repo() {
  mkdir -p "$CACHE_PATH"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a \
      --exclude='.git' \
      --exclude='.venv' \
      --exclude='__pycache__' \
      --exclude='.pytest_cache' \
      "$REPO_ROOT/" "$CACHE_PATH/"
  else
    rm -rf "$CACHE_PATH"
    mkdir -p "$CACHE_PATH"
    tar \
      --exclude='./.git' \
      --exclude='./.venv' \
      --exclude='./__pycache__' \
      --exclude='./.pytest_cache' \
      -C "$REPO_ROOT" -cf - . | tar -C "$CACHE_PATH" -xf -
  fi
}

update_installed_plugins() {
  mkdir -p "$(dirname "$INSTALL_FILE")"
  python3 - "$INSTALL_FILE" "$PLUGIN_ID" "$CACHE_PATH" "$VERSION" "$GIT_SHA" <<'PY'
import datetime
import json
import sys
from pathlib import Path

install_file = Path(sys.argv[1])
plugin_id = sys.argv[2]
cache_path = sys.argv[3]
version = sys.argv[4]
git_sha = sys.argv[5]

if install_file.exists():
    data = json.loads(install_file.read_text())
else:
    data = {}

plugins = data.setdefault("plugins", {})
entries = plugins.setdefault(plugin_id, [{}])
if not entries:
    entries.append({})

for entry in entries:
    entry["installPath"] = cache_path
    entry["version"] = version
    entry["lastUpdated"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    entry["gitCommitSha"] = git_sha

install_file.write_text(json.dumps(data, indent=2) + "\n")
PY
}

restart_claude_daemon() {
  local pid_file="$HOME/.ccmatrix/daemon.pid"
  local pid=""

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
  fi

  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"

  (cd "$CACHE_PATH" && uv run python -m claude_code_matrix.daemon >/dev/null 2>&1 &)
}

run_codex_enable() {
  uv run --project "$CACHE_PATH" codex-matrix enable
}

run_refresh_rooms() {
  uv run --project "$CACHE_PATH/packages/matrix-bridge-common" \
    python -m matrix_bridge.tools.refresh_rooms
}

if [[ "$dry_run" == "true" ]]; then
  echo "DRY-RUN: would mirror repo to $CACHE_PATH"
  echo "DRY-RUN: would update $INSTALL_FILE for $PLUGIN_ID"
  if [[ "$restart_daemon" == "true" ]]; then
    echo "DRY-RUN: would restart Claude Matrix daemon from $CACHE_PATH"
  fi
  echo "DRY-RUN: would run codex-matrix enable from $CACHE_PATH"
  if [[ "$refresh_rooms" == "true" ]]; then
    echo "DRY-RUN: would refresh Matrix room names and avatars"
  fi
  exit 0
fi

copy_repo
update_installed_plugins

if [[ "$restart_daemon" == "true" ]]; then
  restart_claude_daemon
fi

run_codex_enable

if [[ "$refresh_rooms" == "true" ]]; then
  run_refresh_rooms
fi

echo "Sync complete."
