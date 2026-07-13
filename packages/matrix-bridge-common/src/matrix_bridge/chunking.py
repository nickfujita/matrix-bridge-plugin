"""Split long messages into Matrix-sized chunks at natural boundaries.

Matrix caps a single event at 65536 bytes (the whole PDU), and the plain body is
sent alongside an HTML `formatted_body`, so the practical budget for the text is
well under that. Rather than hard-truncating a reply — which loses content and,
since the server-side voicehub speaks `content.body`, also truncates the audio —
long messages are split into several events, each tagged for TTS, so the reader
sees everything and the listener hears everything.

Splits prefer, in order: paragraph break, line break, sentence end, word break.
A fenced code block is kept intact when it fits; when a single block is larger
than one chunk, the fence is closed and reopened across the boundary so both
halves still render as code.
"""

from __future__ import annotations

import re

# Conservative per-chunk budget. The rendered HTML roughly doubles the plain body
# (worse for tables/code), and the event carries both, so this keeps a typical
# event around 24 KB and a pathological one comfortably under the 65536 ceiling.
DEFAULT_MAX_CHARS = 12000

_FENCE_RE = re.compile(r"^\s*```", re.MULTILINE)
# Sentence end: ., !, ? optionally followed by a closing quote/bracket, then
# whitespace — plus the Japanese full stops, which need no trailing space.
_SENTENCE_END_RE = re.compile(r'[.!?][)\]"\'”’]?\s|[。！？]')


def _open_fence_lang(text: str) -> str | None:
    """If `text` ends inside a fenced code block, return the fence's language tag.

    Returns None when the text ends outside a code block.
    """
    fences = _FENCE_RE.findall(text)
    if len(fences) % 2 == 0:
        return None
    # Inside a block: recover the opening fence's info string so we can reopen it.
    last_open = text.rfind("```")
    line_end = text.find("\n", last_open)
    if line_end == -1:
        return ""
    return text[last_open + 3 : line_end].strip()


def _split_point(text: str, limit: int) -> int:
    """Best index to cut `text` at, no greater than `limit`.

    Prefers a paragraph break, then a line break, then a sentence end, then a word
    break; falls back to a hard cut only when the text has no break at all.
    """
    window = text[:limit]

    for sep in ("\n\n", "\n"):
        idx = window.rfind(sep)
        if idx > 0:
            return idx + len(sep)

    matches = list(_SENTENCE_END_RE.finditer(window))
    if matches:
        idx = matches[-1].end()
        if idx > 0:
            return idx

    idx = window.rfind(" ")
    if idx > 0:
        return idx + 1

    return limit  # no natural boundary — hard cut


def split_message(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split `text` into chunks of at most `max_chars`, at natural boundaries.

    Returns [text] unchanged when it already fits, so the common case is a single
    event exactly as before.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    rest = text
    carry_lang: str | None = None  # language of a code fence left open by the last chunk

    while rest:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        budget = max_chars - len(prefix)

        if len(prefix) + len(rest) <= max_chars:
            chunks.append(prefix + rest)
            break

        cut = _split_point(rest, budget)
        piece = prefix + rest[:cut].rstrip()
        rest = rest[cut:].lstrip("\n")

        # If we cut inside a code block, close it here and reopen it next chunk so
        # neither half renders as prose.
        carry_lang = _open_fence_lang(piece)
        if carry_lang is not None:
            piece = piece + "\n```"

        chunks.append(piece)

    return [c for c in chunks if c.strip()]
