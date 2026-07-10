"""VM identity helpers — letter + color derived from the host."""

import os
import socket

# Color palette per machine. Picked to be visually distinct at room-list
# thumbnail size and to avoid clashing with the title-bar status emojis
# (red/black). Machines whose hostname does not end in a distinguishing letter
# (many cloud hosts get random names) MUST set CCMATRIX_VM_LETTER explicitly
# (see vm_letter below). Letters below are examples; add your own as needed.
_VM_COLORS: dict[str, tuple[int, int, int, int]] = {
    "A": (255, 153, 0, 255),    # amber
    "B": (0, 194, 209, 255),    # teal
    "C": (224, 57, 158, 255),   # magenta
    "D": (140, 90, 230, 255),   # purple
    "E": (90, 180, 90, 255),    # green
    "F": (33, 150, 243, 255),   # blue
    "G": (255, 111, 145, 255),  # coral pink
    "H": (156, 39, 176, 255),   # violet
    "S": (255, 214, 10, 255),   # yellow
}

# Fallback palette used when a hostname maps to an unknown letter. Indexed by
# ord(letter) % len so different letters at least get different colors.
_FALLBACK_PALETTE: list[tuple[int, int, int, int]] = [
    (255, 153, 0, 255),
    (0, 194, 209, 255),
    (224, 57, 158, 255),
    (140, 90, 230, 255),    # purple
    (90, 180, 90, 255),     # green (avoid pure status-green)
    (230, 140, 30, 255),    # orange-red
]


def vm_letter() -> str:
    """Return a single-character machine identifier.

    By default the letter is derived from the hostname — the last alphabetic
    character, uppercased (`workstation-a` → `A`, `workstation-b` → `B`).

    On hosts whose names are random or provider-assigned (common on cloud
    machines) hostname-derived letters are unreliable, so `CCMATRIX_VM_LETTER`
    is REQUIRED there and overrides auto-detection. Set it in the environment
    before anything touches the bridge, e.g. `export CCMATRIX_VM_LETTER=S`.
    """
    override = os.environ.get("CCMATRIX_VM_LETTER")
    if override:
        return override.strip().upper()[:1] or "?"

    hostname = socket.gethostname()
    # Trim trailing digit-suffixes or dots; take last alpha char
    for ch in reversed(hostname):
        if ch.isalpha():
            return ch.upper()
    return "?"


def vm_color(letter: str | None = None) -> tuple[int, int, int, int]:
    """Return the RGBA fill color for a VM letter."""
    letter = (letter or vm_letter()).upper()
    if letter in _VM_COLORS:
        return _VM_COLORS[letter]
    return _FALLBACK_PALETTE[ord(letter[0]) % len(_FALLBACK_PALETTE)]
