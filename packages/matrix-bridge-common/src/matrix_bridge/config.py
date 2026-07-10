"""Configuration management for the Matrix bridge."""

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import stat


CONFIG_DIR = Path.home() / ".ccmatrix"
STATE_DIR = CONFIG_DIR  # Alias used by other modules
CONFIG_FILE = CONFIG_DIR / "config.json"

# Keys older configs may carry that this version no longer uses. They are
# dropped on load and the file is re-saved without them (migration shim).
#   admin_access_token — impersonated sends were removed (scoped bot token only)
#   voice_service_url  — VM-side TTS/STT removed (server-side voicehub owns voice)
_LEGACY_KEYS = ("admin_access_token", "voice_service_url")


@dataclass
class MatrixConfig:
    homeserver: str  # e.g. "https://matrix.example.com"
    user_id: str  # e.g. "@bot:example.com"
    access_token: str  # Bot's scoped access token
    admin_user_id: str  # Human user to auto-invite to session rooms
    device_id: str = "CCMATRIX"
    # Tag the final assistant message with "cc.tts" so the server-side voicehub
    # synthesizes it. When False, no tag is emitted (kill switch, not a
    # resurrection of client-side synthesis).
    server_side_voice: bool = True
    # Outbound HTTP proxy for every Matrix request. Machines that reach the
    # homeserver through a local forward proxy set e.g. "http://127.0.0.1:1055";
    # machines with a direct connection leave this blank.
    proxy_url: str = ""
    # Map a git repo name to a friendlier room-name label. Empty by default;
    # override in config.json, e.g. {"my-really-long-repo-name": "myrepo"}.
    repo_aliases: dict[str, str] = field(default_factory=dict)


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    return val.strip().lower() not in ("0", "false", "no", "off")


def _ensure_secure_mode(path: Path) -> None:
    """Chmod the config to 0600 if it is currently more permissive.

    One-time fix for pre-0.5 installs that wrote config.json at the umask
    default (usually 0644), leaving the bot token world-readable.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            path.chmod(0o600)
    except OSError:
        pass


def load_config() -> MatrixConfig | None:
    """Load config from ~/.ccmatrix/config.json or environment."""
    # Environment variables take precedence
    if os.environ.get("CCMATRIX_HOMESERVER"):
        admin = os.environ.get("CCMATRIX_ADMIN_USER_ID")
        if not admin:
            return None
        return MatrixConfig(
            homeserver=os.environ["CCMATRIX_HOMESERVER"],
            user_id=os.environ["CCMATRIX_USER_ID"],
            access_token=os.environ["CCMATRIX_ACCESS_TOKEN"],
            admin_user_id=admin,
            device_id=os.environ.get("CCMATRIX_DEVICE_ID", "CCMATRIX"),
            server_side_voice=_env_bool("CCMATRIX_SERVER_SIDE_VOICE", True),
            proxy_url=os.environ.get("CCMATRIX_PROXY_URL", ""),
        )

    if not CONFIG_FILE.exists():
        return None

    data = json.loads(CONFIG_FILE.read_text())

    # Old configs have room_id instead of admin_user_id — require re-setup
    if "admin_user_id" not in data:
        return None

    # Remove legacy keys
    data.pop("room_id", None)

    # Migration shim: drop removed keys, remember whether any were present so we
    # can re-save the file cleanly below.
    legacy_present = any(k in data for k in _LEGACY_KEYS)
    for k in _LEGACY_KEYS:
        data.pop(k, None)

    # Filter unknown keys so older/newer configs don't fail dataclass instantiation
    known = {f.name for f in MatrixConfig.__dataclass_fields__.values()}
    data = {k: v for k, v in data.items() if k in known}

    config = MatrixConfig(**data)

    # One-time permission tightening for pre-0.5 installs written at 0644.
    _ensure_secure_mode(CONFIG_FILE)

    # Rewrite without the legacy keys so the on-disk file stays clean.
    if legacy_present:
        save_config(config)

    return config


def save_config(config: MatrixConfig) -> None:
    """Persist config to ~/.ccmatrix/config.json with 0600 permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "homeserver": config.homeserver,
        "user_id": config.user_id,
        "access_token": config.access_token,
        "admin_user_id": config.admin_user_id,
        "device_id": config.device_id,
        "server_side_voice": config.server_side_voice,
    }
    if config.proxy_url:
        data["proxy_url"] = config.proxy_url
    if config.repo_aliases:
        data["repo_aliases"] = config.repo_aliases
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    _ensure_secure_mode(CONFIG_FILE)
