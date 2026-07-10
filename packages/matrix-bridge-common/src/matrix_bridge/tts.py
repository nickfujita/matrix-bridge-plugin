"""TTS helpers — text cleaning, language detection, and chunking.

These are pure text utilities. Actual speech synthesis is performed
server-side by the companion `voice-server` service (an OpenAI-compatible
TTS/STT HTTP API), invoked by the `matrix-voicehub` appservice when it sees a
message tagged `cc.tts`. This module only prepares and routes the text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)

_JP_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_KAKASI = None
_NEUTRAL_PREFIX_CHARS = set("\ufeff \t\n\r>・*-+0123456789.．、,，:：;；'\"“”‘’([{【「『")

@dataclass(frozen=True)
class TTSRoute:
    """Prepared text and target engine for one cleaned TTS chunk."""

    engine: Literal["kokoro", "aivis"]
    text: str
    reason: str


@dataclass(frozen=True)
class TTSPart:
    """A segment of text routed to a specific language engine."""

    text: str
    lang: Literal["en", "ja"]


def split_mixed_text(text: str) -> list[TTSPart]:
    """Auto-segment mixed text into alternating English (en) and Japanese (ja) parts."""
    if not has_japanese(text):
        return [TTSPart(text, "en")]

    jp_len = count_japanese_chars(text)
    latin_len = count_latin_letters(text)
    if latin_len == 0:
        return [TTSPart(text, "ja")]

    # If CJK density is high, treat the whole text as a single Japanese segment
    # (e.g. "APIの実装はほぼ完了しています。")
    density = jp_len / (jp_len + latin_len)
    if density >= 0.3:
        return [TTSPart(text, "ja")]

    # Matches CJK script sequences and fullwidth forms
    cjk_pattern = re.compile(
        r"([\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]+)"
    )

    parts = cjk_pattern.split(text)
    segments: list[TTSPart] = []
    for i, part in enumerate(parts):
        if not part:
            continue
        is_jp = i % 2 == 1
        if is_jp:
            segments.append(TTSPart(part.strip(), "ja"))
        else:
            # Only keep as a standalone segment if it has printable alphanumeric characters
            if any(c.isalnum() for c in part):
                segments.append(TTSPart(part, "en"))
            else:
                if segments:
                    # Reconstruct frozen last element with trailing punctuation
                    last = segments[-1]
                    segments[-1] = TTSPart(last.text + part, last.lang)
    return segments



def has_japanese(text: str) -> bool:
    """Return True when text contains Japanese kana/kanji characters.

    This deliberately excludes plain Latin letters. AivisSpeech handles Latin
    technical terms inside Japanese sentences well, but English-only prose
    should stay on Kokoro.
    """
    return bool(_JP_RE.search(text))


def count_japanese_chars(text: str) -> int:
    """Count Japanese kana/kanji characters, excluding Latin letters."""
    return len(_JP_RE.findall(text))


def count_latin_letters(text: str) -> int:
    """Count Latin letters as a proxy for English-dominant prose."""
    return len(_LATIN_RE.findall(text))


def _first_meaningful_script(text: str) -> Literal["japanese", "latin", "other", "none"]:
    """Return the script of the first meaningful character in a paragraph."""
    for ch in text.strip():
        if ch in _NEUTRAL_PREFIX_CHARS:
            continue
        if has_japanese(ch):
            return "japanese"
        if _LATIN_RE.fullmatch(ch):
            return "latin"
        if ch.isalnum():
            return "other"
    return "none"


def _last_meaningful_script(text: str) -> Literal["japanese", "latin", "other", "none"]:
    """Return the script of the last meaningful character in a paragraph."""
    neutral_suffix_chars = _NEUTRAL_PREFIX_CHARS | set("。.!?！？)）]}】」』\"”’")
    for ch in reversed(text.strip()):
        if ch in neutral_suffix_chars:
            continue
        if has_japanese(ch):
            return "japanese"
        if _LATIN_RE.fullmatch(ch):
            return "latin"
        if ch.isalnum():
            return "other"
    return "none"


def is_japanese_dominant(text: str) -> bool:
    """Heuristic for routing a chunk to Japanese TTS.

    Uses a CJK density check (>= 30%) to decide if the chunk should route entirely
    to Aivis. If it contains Japanese but is mostly English, it is marked as
    English-dominant and is split/stitched at the phrase level.
    """
    if not has_japanese(text):
        return False

    first_script = _first_meaningful_script(text)
    if first_script == "japanese":
        return True

    # Pure Japanese snippets have no Latin first-character cue.
    if count_latin_letters(text) == 0:
        return True

    jp_len = count_japanese_chars(text)
    latin_len = count_latin_letters(text)
    density = jp_len / (jp_len + latin_len)
    return density >= 0.3


def _get_kakasi():
    global _KAKASI
    if _KAKASI is None:
        import pykakasi

        _KAKASI = pykakasi.kakasi()
    return _KAKASI


def romanize_japanese_for_kokoro(text: str) -> str:
    """Replace Japanese runs with rough Hepburn romaji for English TTS.

    This is intentionally a fallback, not the learning-quality path. It keeps
    Kokoro from saying "Japanese character" or dropping text in English-heavy
    chunks with stray Japanese terms. Japanese learning examples should be
    separated into their own blocks so they route to Aivis instead.
    """
    if not has_japanese(text):
        return text

    converted = _get_kakasi().convert(text)
    output: list[str] = []
    previous_was_romaji = False

    def append_space_if_needed() -> None:
        if output and not output[-1].endswith((" ", "\n", "\t", "(", "[", "{", '"', "'")):
            output.append(" ")

    for item in converted:
        orig = item.get("orig", "")
        if not orig:
            continue

        if has_japanese(orig):
            replacement = (item.get("hepburn") or item.get("passport") or orig).strip()
            if not replacement:
                continue
            append_space_if_needed()
            output.append(replacement)
            previous_was_romaji = True
            continue

        replacement = orig.replace("、", ", ").replace("。", ".")
        replacement = replacement.replace("「", '"').replace("」", '"')
        replacement = replacement.replace("（", "(").replace("）", ")")

        if previous_was_romaji and replacement and not replacement[0].isspace() and replacement[0] not in ".,!?;:)]}、。！？":
            output.append(" ")
        output.append(replacement)
        previous_was_romaji = False

    result = "".join(output)
    result = re.sub(r"[ \t]+", " ", result)
    result = re.sub(r"\s+([.,!?;:])", r"\1", result)
    result = re.sub(r"([([{])\s+", r"\1", result)
    return result.strip()


def prepare_tts_route(text: str, inline_romaji_enabled: bool = True) -> TTSRoute:
    """Choose the TTS engine and any text transformation for a cleaned chunk."""
    if not has_japanese(text):
        return TTSRoute(engine="kokoro", text=text, reason="english_or_non_japanese")

    if is_japanese_dominant(text):
        return TTSRoute(engine="aivis", text=text, reason="japanese_dominant")

    if inline_romaji_enabled:
        return TTSRoute(
            engine="kokoro",
            text=romanize_japanese_for_kokoro(text),
            reason="english_dominant_inline_romaji",
        )

    return TTSRoute(engine="aivis", text=text, reason="japanese_present_romaji_disabled")


def split_for_tts(text: str, max_words: int = 180, max_chars: int = 1800) -> list[str]:
    """Split text at paragraph/sentence boundaries to stay within Kokoro's sweet spot.

    Mirrors paper-voice's tts_batch.split_script — pre-chunking at the caller
    keeps Kokoro quality high on long inputs and stops the voice server from
    holding a single giant audio tensor in memory.
    """

    def _sentences(s: str) -> list[str]:
        parts = re.split(r'(?<=[.!?])\s+(?=[A-Z"])', s)
        return [p.strip() for p in parts if p.strip()]

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current_parts: list[str] = []
    current_words = 0
    current_chars = 0

    def _flush():
        nonlocal current_parts, current_words, current_chars
        if current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = []
            current_words = 0
            current_chars = 0

    for para in paragraphs:
        para_words = len(para.split())
        para_chars = len(para)

        if para_words > max_words or para_chars > max_chars:
            _flush()
            sent_parts: list[str] = []
            sent_words = 0
            sent_chars = 0
            for sent in _sentences(para):
                sw, sc = len(sent.split()), len(sent)
                if sent_parts and (sent_words + sw > max_words or sent_chars + sc > max_chars):
                    chunks.append(" ".join(sent_parts))
                    sent_parts, sent_words, sent_chars = [], 0, 0
                sent_parts.append(sent)
                sent_words += sw
                sent_chars += sc
            if sent_parts:
                current_parts.append(" ".join(sent_parts))
                current_words += sent_words
                current_chars += sent_chars
            continue

        if current_parts and (current_words + para_words > max_words or current_chars + para_chars > max_chars):
            _flush()
        current_parts.append(para)
        current_words += para_words
        current_chars += para_chars

    _flush()
    return chunks or [text]


def _split_into_sentences(text: str) -> list[str]:
    """Split text into sentences using English and Japanese sentence boundaries."""
    # Split on English/Japanese sentence terminals (. ! ? 。 ！ ？)
    # followed by optional space. Avoids breaking decimals/abbreviations.
    pattern = r"(?:(?<=[.!?])\s+(?=[A-Z\"「]))|(?:(?<=[。！？])\s*)"
    parts = re.split(pattern, text)
    return [p.strip() for p in parts if p.strip()]


def split_for_tts_preserve_language(text: str, max_words: int = 180, max_chars: int = 1800) -> list[str]:
    """Split for TTS while avoiding English/Japanese paragraph mixing.

    The voice server routes each chunk independently. If a paragraph contains
    Japanese characters, it is split sentence-by-sentence to ensure precise routing
    and phrase stitching of mixed/bilingual content.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return [text] if text else []

    chunks: list[str] = []
    for paragraph in paragraphs:
        if has_japanese(paragraph):
            chunks.extend(_split_into_sentences(paragraph))
        else:
            chunks.extend(split_for_tts(paragraph, max_words=max_words, max_chars=max_chars))
    return chunks or [text]


def clean_for_tts(text: str) -> str:
    """Strip markdown, code, and special characters that sound bad in TTS."""

    # Preserve explicitly Japanese fenced blocks for TTS, but remove other code.
    # This gives authors an explicit escape hatch: ```ja or ```japanese.
    def _clean_fenced(match: re.Match) -> str:
        lang = (match.group(1) or "").strip().lower()
        body = match.group(2).strip()
        if lang in {"ja", "jp", "japanese", "日本語"}:
            return f"\n{body}\n"
        return ""

    text = re.sub(r"```([^\n`]*)?\n([\s\S]*?)```", _clean_fenced, text)

    # Remove inline code backticks
    text = re.sub(r"`([^`]*)`", r"\1", text)

    # Remove markdown bold/italic markers
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)

    # Remove markdown headers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # Remove markdown links, keep label
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Remove bare URLs
    text = re.sub(r"https?://\S+", "", text)

    # Arrows → natural language
    text = text.replace("→", " to ")
    text = text.replace("->", " to ")
    text = text.replace("=>", " gives ")
    text = text.replace("←", " from ")
    text = text.replace("<-", " from ")

    # Pipe separators
    text = text.replace(" | ", ", ")

    # Markdown horizontal rules
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\*{3,}$", "", text, flags=re.MULTILINE)

    # Markdown bullet points
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)

    # Numbered lists — keep the number
    text = re.sub(r"^\s*(\d+)\.\s+", r"\1. ", text, flags=re.MULTILINE)

    # Emoji status indicators used in room names
    text = text.replace("🟢", "").replace("⏸️", "").replace("✅", "")

    # Underscores in identifiers → spaces (room_send → room send)
    text = re.sub(r"(?<=[a-z])_(?=[a-z])", " ", text)

    # Collapse multiple newlines/spaces
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"  +", " ", text)

    return text.strip()
