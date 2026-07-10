import unittest
from importlib.resources import files

from matrix_bridge.avatars import _AGENT_BADGE_BACKGROUND_COLORS, _LOGO_FILES, RING_COLOR, _composite


class AntigravityAvatarTests(unittest.TestCase):
    def test_antigravity_uses_gemini_logo_asset_and_renders_png(self):
        self.assertEqual(_LOGO_FILES["antigravity"], "gemini-logo.png")
        self.assertEqual(_AGENT_BADGE_BACKGROUND_COLORS["antigravity"], RING_COLOR)
        logo = files("matrix_bridge.assets").joinpath("gemini-logo.png").read_bytes()
        self.assertTrue(logo.startswith(b"\x89PNG"))

        png = _composite("antigravity", "C")
        self.assertGreater(len(png), 1000)
        self.assertTrue(png.startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
