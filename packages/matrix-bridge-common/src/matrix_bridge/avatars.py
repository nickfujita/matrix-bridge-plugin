"""Room avatar generation — composite VM letter + framework badge."""

import io
import json
import logging
from importlib.resources import files
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .matrix import MatrixClient
from .vm import vm_color, vm_letter as detect_vm_letter

logger = logging.getLogger(__name__)

CACHE_DIR = Path.home() / ".ccmatrix"
AVATAR_CACHE = CACHE_DIR / "avatar-cache.json"

CANVAS_SIZE = 256
RING_COLOR = (20, 20, 20, 255)
BADGE_SIZE = 96
BADGE_INSET = 20  # pull badge in from bottom-right corner
LETTER_FONT_SIZE = 260
LETTER_NUDGE_X = 12  # shift letter up-left to balance the badge
LETTER_NUDGE_Y = 24
RING_WIDTH = 4

# Per-letter optical-center adjustments on top of bbox centering. Some
# letterforms (C, J, …) have asymmetric visible mass within their bbox
# and look off-center when bbox-centered. Adjust with care.
_LETTER_OPTICAL_NUDGE: dict[str, tuple[int, int]] = {
    "C": (-8, 0),
}

_LOGO_FILES = {
    "claude": "anthropic-logo.png",
    "codex": "openai-logo.png",
    "antigravity": "gemini-logo.png",
}

_AGENT_BADGE_BACKGROUND_COLORS = {
    # Keep the VM tile color unchanged, but render the Gemini sparkle badge on
    # a dark background so it reads more like the Claude/OpenAI badges.
    "antigravity": RING_COLOR,
}


def _load_cache() -> dict[str, str]:
    if AVATAR_CACHE.exists():
        return json.loads(AVATAR_CACHE.read_text())
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_CACHE.write_text(json.dumps(cache))


def _load_asset_bytes(filename: str) -> bytes:
    return files("matrix_bridge.assets").joinpath(filename).read_bytes()


def _composite(agent: str, letter: str) -> bytes:
    """Render a 256x256 PNG: VM-color tile + Inter Black letter + framework badge."""
    bg_color = vm_color(letter)

    canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), bg_color)
    draw = ImageDraw.Draw(canvas)

    # VM letter, near-canvas-filling, nudged up-left to balance the badge
    font_bytes = _load_asset_bytes("Inter-Black.ttf")
    font = ImageFont.truetype(io.BytesIO(font_bytes), LETTER_FONT_SIZE)
    glyph = letter.upper()
    bbox = draw.textbbox((0, 0), glyph, font=font)
    lw, lh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    opt_dx, opt_dy = _LETTER_OPTICAL_NUDGE.get(glyph, (0, 0))
    lx = (CANVAS_SIZE - lw) / 2 - bbox[0] - LETTER_NUDGE_X + opt_dx
    ly = (CANVAS_SIZE - lh) / 2 - bbox[1] - LETTER_NUDGE_Y + opt_dy
    draw.text((lx, ly), glyph, fill=RING_COLOR, font=font)

    # Framework-logo badge: circle in bottom-right, pulled in from the corner
    bx1 = CANVAS_SIZE - BADGE_INSET
    by1 = CANVAS_SIZE - BADGE_INSET
    bx0 = bx1 - BADGE_SIZE
    by0 = by1 - BADGE_SIZE
    draw.ellipse(
        (bx0 - RING_WIDTH, by0 - RING_WIDTH, bx1 + RING_WIDTH, by1 + RING_WIDTH),
        fill=RING_COLOR,
    )

    logo_bytes = _load_asset_bytes(_LOGO_FILES[agent])
    logo = Image.open(io.BytesIO(logo_bytes)).convert("RGBA")
    logo.thumbnail((BADGE_SIZE, BADGE_SIZE), Image.LANCZOS)
    badge_bg = _AGENT_BADGE_BACKGROUND_COLORS.get(agent, (255, 255, 255, 255))
    badge_canvas = Image.new("RGBA", (BADGE_SIZE, BADGE_SIZE), badge_bg)
    px = (BADGE_SIZE - logo.width) // 2
    py = (BADGE_SIZE - logo.height) // 2
    badge_canvas.paste(logo, (px, py), logo)
    logo = badge_canvas

    mask = Image.new("L", (BADGE_SIZE, BADGE_SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, BADGE_SIZE, BADGE_SIZE), fill=255)
    canvas.paste(logo, (bx0, by0), mask)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, "PNG")
    return buf.getvalue()


async def get_avatar_mxc(
    client: MatrixClient,
    agent: str,
    letter: str | None = None,
) -> str | None:
    """Return mxc:// URI for the (agent, vm_letter) avatar, generating on demand.

    Cached in ~/.ccmatrix/avatar-cache.json so repeat calls don't re-upload.
    """
    if agent not in _LOGO_FILES:
        logger.error(f"Unknown agent: {agent}")
        return None

    letter = (letter or detect_vm_letter()).upper()
    cache = _load_cache()
    cache_key = f"avatar_{agent}_{letter}"

    if cache_key in cache:
        if await client.download(cache[cache_key]):
            return cache[cache_key]
        logger.warning(f"Stale avatar URL, re-uploading: {cache[cache_key]}")
        del cache[cache_key]
        _save_cache(cache)

    try:
        png_bytes = _composite(agent, letter)
    except Exception as e:
        logger.error(f"avatar composite failed for {agent}/{letter}: {e}")
        return None

    filename = f"avatar-{agent}-{letter}.png"
    mxc_url = await client.upload(png_bytes, "image/png", filename)
    if mxc_url:
        cache[cache_key] = mxc_url
        _save_cache(cache)
        logger.info(f"Uploaded avatar {agent}/{letter}: {mxc_url}")
    return mxc_url
