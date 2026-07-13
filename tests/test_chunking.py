"""Long messages must be split, not truncated — text AND audio must be complete.

Regression context: assistant replies were hard-cut at 4000 chars. Readers lost
the tail, and once TTS moved server-side (voicehub speaks `content.body`), the
audio got cut too — previously the plugin synthesized from the full text, so the
truncation was text-only.
"""

import unittest

from matrix_bridge.chunking import DEFAULT_MAX_CHARS, split_message


class SplitMessageTests(unittest.TestCase):
    def test_short_message_is_untouched(self):
        self.assertEqual(split_message("hello"), ["hello"])

    def test_empty_message_yields_nothing(self):
        self.assertEqual(split_message(""), [])

    def test_nothing_is_lost(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 200 for i in range(30))
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1, "this input must actually split")
        # every word survives, in order
        self.assertEqual("".join(text.split()), "".join("".join(c.split()) for c in chunks))

    def test_every_chunk_fits_the_budget(self):
        text = "word " * 20000
        for chunk in split_message(text):
            self.assertLessEqual(len(chunk), DEFAULT_MAX_CHARS)

    def test_splits_on_paragraph_boundary(self):
        a = "A" * (DEFAULT_MAX_CHARS - 10)   # leaves no room for the next paragraph
        text = f"{a}\n\nSecond paragraph."
        chunks = split_message(text)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("A"))
        self.assertEqual(chunks[1], "Second paragraph.")

    def test_falls_back_to_sentence_boundary(self):
        # one long paragraph, no newlines: must break after a sentence, not mid-word
        sentences = "This is a sentence. " * 1200
        chunks = split_message(sentences)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks[:-1]:
            self.assertTrue(
                chunk.rstrip().endswith("."),
                f"chunk should end at a sentence, got: ...{chunk[-40:]!r}",
            )

    def test_never_splits_mid_word(self):
        text = "supercalifragilistic " * 2000
        for chunk in split_message(text)[:-1]:
            self.assertFalse(chunk.rstrip().endswith("supercalifragilisti"))
            self.assertTrue(chunk.rstrip().endswith("supercalifragilistic"))

    def test_code_fence_is_reopened_across_a_split(self):
        code = "\n".join(f"line_{i} = {i}" for i in range(2000))
        text = f"Here is code:\n\n```python\n{code}\n```\n"
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        # every chunk must have balanced fences, so neither half renders as prose
        for chunk in chunks:
            self.assertEqual(
                chunk.count("```") % 2, 0,
                f"unbalanced code fence in chunk: {chunk[:60]!r}...",
            )
        # the continuation chunk must reopen the python fence
        self.assertTrue(chunks[1].startswith("```python"))

    def test_hard_cut_when_there_is_no_boundary_at_all(self):
        text = "x" * (DEFAULT_MAX_CHARS * 2 + 5)
        chunks = split_message(text)
        self.assertEqual(sum(len(c) for c in chunks), len(text))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), DEFAULT_MAX_CHARS)

    def test_a_long_reply_still_fits_a_single_event(self):
        """The old 4000-char cap was 3x smaller than needed; verify the new budget
        keeps a realistic long reply in ONE chunk (no gratuitous splitting)."""
        realistic = "This is a fairly long assistant reply. " * 250  # ~9.5k chars
        self.assertEqual(len(split_message(realistic)), 1)


if __name__ == "__main__":
    unittest.main()
