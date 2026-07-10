"""Room-name composition: `[emoji] {repo}[/{branch}]`."""

import subprocess
from pathlib import Path

# Title-bar lifecycle markers. Active rooms intentionally have no prefix so
# the repo/branch text gets maximum room-list width; ended rooms get a red dot.
STATUS_ACTIVE = ""              # no emoji
STATUS_ENDED = "\U0001f534"     # 🔴


def detect_branch(cwd: str | Path) -> str | None:
    """Return current git branch for cwd, or None if not a repo / detached."""
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":  # detached
        return None
    return branch


def _detect_repo_from_origin(cwd: str | Path) -> str | None:
    """Parse the repo name out of `git remote get-url origin`. None if unavailable."""
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    # Last path segment, also splitting on ":" so ssh URLs (git@host:user/repo.git) work
    name = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name or None


def repo_name_from_cwd(cwd: str | Path, aliases: dict[str, str] | None = None) -> str:
    """Canonical repo name (from git origin), falling back to cwd basename.

    A folder named `myrepo-copy` that clones the `myrepo` repo will surface as
    `myrepo` (the origin URL's repo name wins over the directory name). The
    alias map then applies on top.
    """
    name = _detect_repo_from_origin(cwd) or (Path(cwd).name if cwd else "unknown")
    if aliases and name in aliases:
        return aliases[name]
    return name


def build_room_name(
    cwd: str | Path,
    status: str = STATUS_ACTIVE,
    repo_aliases: dict[str, str] | None = None,
    branch: str | None = None,
) -> str:
    """Compose the room name. Caller may pass branch explicitly to avoid re-shell."""
    repo = repo_name_from_cwd(cwd, repo_aliases)
    if branch is None:
        branch = detect_branch(cwd)

    body = f"{repo}/{branch}" if branch else repo
    if status:
        return f"{status} {body}"
    return body
