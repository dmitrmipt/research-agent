import unittest
from unittest.mock import patch

from materials import render_markdown_preview
from math_markup import MATH_HEADER, escape_markdown


class MathMarkupTest(unittest.TestCase):
    def test_missing_markdown_renderer_offers_download_and_setup(self):
        with patch.dict("sys.modules", {"markdown_it": None}):
            html = render_markdown_preview(b"# Article title", "current revision")
        self.assertIn("pip install -r requirements.txt", html)
        self.assertIn('href="profile.md?snapshot=current%20revision"', html)
        self.assertNotIn("# Article title", html)

    def test_legacy_markdown_decodes_escaped_tex_inside_profile_sections(self):
        old = ("## Краткий обзор\n\n" + r"Формула $$\\frac{x\_1}{y}$$." +
               "\n\n## Список литературы\n\n" + r"Запись $x\_1$.")
        html = render_markdown_preview(old.encode(), "")
        self.assertIn(r'<span class="profile-math-source">$$\frac{x_1}{y}$$</span>', html)
        self.assertEqual(html.count('class="profile-math-source"'), 1)
        self.assertIn("Запись $x_1$.", html)

    def test_preview_preserves_currency_and_code_and_escapes_markup_inside_math(self):
        paragraph = escape_markdown(r"Цены $0.10 и $0.50. Формула $x<y$.", "display")
        source = MATH_HEADER + paragraph + "\n\n`$x_i$`\n\n$\\text{<script>}$."
        html = render_markdown_preview(source.encode(), "")
        self.assertIn("Цены $0.10 и $0.50.", html)
        self.assertIn('<span class="profile-math-source">$x&lt;y$</span>', html)
        self.assertIn('<code>$x_i$</code>', html)
        self.assertIn(r"$\text{&lt;script&gt;}$", html)
        self.assertNotIn("<script>", html)
