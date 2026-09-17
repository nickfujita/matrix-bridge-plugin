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


def _detect_repo_root(cwd: str | Path) -> str | None:
    """Top-level directory of the repository containing cwd. None outside git."""
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def repo_name_from_cwd(
    cwd: str | Path, aliases: dict[str, str] | None = None, *, git_only: bool = False,
) -> str:
    """Canonical repo name (from git origin), falling back to cwd basename.

    A folder named `myrepo-copy` that clones the `myrepo` repo will surface as
    `myrepo` (the origin URL's repo name wins over the directory name). The
    alias map then applies on top.
    """
    name = _detect_repo_from_origin(cwd)
    if not name and git_only:
        # A remote is optional. Resolve the root so subdirectories still use
        # the repository name, while ordinary directories have no prefix.
        root = _detect_repo_root(cwd)
        if not root:
            return ""
        name = Path(root).name
    name = name or (Path(cwd).name if cwd else "unknown")
    if aliases and name in aliases:
        return aliases[name]
    return name


def build_room_name(
    cwd: str | Path,
    status: str = STATUS_ACTIVE,
    repo_aliases: dict[str, str] | None = None,
    branch: str | None = None,
    title: str | None = None,
    agent: str | None = None,
    session_id: str | None = None,
) -> str:
    """Compose the room name. Caller may pass branch explicitly to avoid re-shell."""
    if title is None and agent and session_id:
        from .session_title import native_title
        title = native_title(agent, session_id)
    repo = repo_name_from_cwd(cwd, repo_aliases, git_only=True)
    if title:
        from .session_title import clean_title
        body = f"{repo} · {clean_title(title)}" if repo else clean_title(title)
        return f"{status} {body}" if status else body
    if branch is None:
        branch = detect_branch(cwd)

    body = (f"{repo}/{branch}" if branch else repo) if repo else "Agent session"
    if status:
        return f"{status} {body}"
    return body
