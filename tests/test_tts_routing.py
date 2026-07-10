import unittest

from matrix_bridge.tts import (
    clean_for_tts,
    is_japanese_dominant,
    prepare_tts_route,
    romanize_japanese_for_kokoro,
)


class TTSRoutingTests(unittest.TestCase):
    def test_english_without_japanese_uses_kokoro(self):
        route = prepare_tts_route("This is an English paragraph about database schemas.")
        self.assertEqual(route.engine, "kokoro")
        self.assertEqual(route.reason, "english_or_non_japanese")

    def test_english_start_with_inline_japanese_gets_romaji_for_kokoro(self):
        route = prepare_tts_route("The term 要件 means requirements in this context.")
        self.assertEqual(route.engine, "kokoro")
        self.assertEqual(route.reason, "english_dominant_inline_romaji")
        self.assertNotIn("要件", route.text)
        self.assertIn("youken", route.text)

    def test_japanese_start_with_english_terms_uses_aivis(self):
        text = "今日は database schema と API integration の要件を確認します。"
        route = prepare_tts_route(text)
        self.assertEqual(route.engine, "aivis")
        self.assertEqual(route.text, text)
        self.assertEqual(route.reason, "japanese_dominant")

    def test_english_start_with_japanese_grammar_uses_aivis(self):
        text = "database schema について確認します"
        route = prepare_tts_route(text)
        self.assertEqual(route.engine, "aivis")
        self.assertEqual(route.text, text)

    def test_english_explanation_of_japanese_particle_stays_kokoro(self):
        text = "The particle は marks the topic"
        route = prepare_tts_route(text)
        self.assertEqual(route.engine, "kokoro")
        self.assertIn("ha", route.text)

    def test_english_explanation_of_japanese_grammar_stays_kokoro(self):
        text = "If it starts English but has strong Japanese grammar like について or 確認します, it goes to Aivis"
        route = prepare_tts_route(text)
        self.assertEqual(route.engine, "kokoro")
        self.assertIn("nitsuite", route.text)

    def test_english_start_ending_japanese_uses_aivis(self):
        text = "database schema について確認します"
        route = prepare_tts_route(text)
        self.assertEqual(route.engine, "aivis")

    def test_english_start_with_japanese_term_and_japanese_ending_uses_aivis(self):
        # This is the accepted tradeoff for the simple final-character rule.
        text = "Requirements are called 要件."
        route = prepare_tts_route(text)
        self.assertEqual(route.engine, "aivis")

    def test_pure_short_japanese_uses_aivis(self):
        route = prepare_tts_route("要件")
        self.assertEqual(route.engine, "aivis")

    def test_romaji_fallback_can_be_disabled(self):
        text = "The term 要件 means requirements."
        route = prepare_tts_route(text, inline_romaji_enabled=False)
        self.assertEqual(route.engine, "aivis")
        self.assertEqual(route.text, text)
        self.assertEqual(route.reason, "japanese_present_romaji_disabled")

    def test_romanize_preserves_english_and_replaces_japanese_phrase(self):
        text = "In Japanese, 要件を整理する means organize requirements."
        out = romanize_japanese_for_kokoro(text)
        self.assertIn("In Japanese", out)
        self.assertIn("youken", out)
        self.assertIn("seiri", out)
        self.assertNotIn("要件", out)
        self.assertNotIn("整理", out)

    def test_structure_heuristic_not_character_count_primary(self):
        self.assertFalse(is_japanese_dominant("The term 要件 means requirements."))
        self.assertTrue(is_japanese_dominant("要件を整理するために、現在の data flow を確認させてください。"))
        self.assertTrue(is_japanese_dominant("API integration について、担当エンジニアに確認します"))
        self.assertTrue(is_japanese_dominant("The Japanese phrase is について"))

    def test_japanese_fenced_block_is_preserved_for_tts(self):
        text = "Intro.\n\n```ja\n要件を整理します。\n```\n\n```python\nprint('not spoken')\n```"
        cleaned = clean_for_tts(text)
        self.assertIn("要件を整理します。", cleaned)
        self.assertNotIn("print", cleaned)
        self.assertNotIn("```", cleaned)


if __name__ == "__main__":
    unittest.main()
