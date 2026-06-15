"""Tests for the dashboard's own Markdown renderer (com2tty...._markdown).

The renderer turns Markdown into ``(Content, anchors)``; ``Content.plain`` is the
copy-friendly text (markup stripped, code verbatim), ``Content.spans`` carry the
styling (and the link ``@click`` actions), and ``anchors`` maps each heading's
slug to its line index for table-of-contents jumps. No Markdown library is
imported -- this is all hand-rolled.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from textual.style import Style

from com2tty.windows.dashboard._markdown import render_markdown


def _styles_over(content, substring):
    """Every span style covering the first char of ``substring``."""
    idx = content.plain.find(substring)
    assert idx != -1, "substring %r not found in %r" % (substring, content.plain)
    return [span.style for span in content.spans
            if span.start <= idx < span.end]


def _has(content, substring, needle):
    return any(isinstance(s, str) and needle in s
               for s in _styles_over(content, substring))


def _link_meta(content, substring):
    """The ``@click`` action string of the link span over ``substring`` (or None)."""
    for style in _styles_over(content, substring):
        if isinstance(style, Style) and style.meta.get("@click"):
            return style.meta["@click"]
    return None


class TestInline(unittest.TestCase):

    def test_bold_italic_bolditalic(self):
        content, _ = render_markdown("a **bold** b *italic* c ***both*** d")
        self.assertEqual(content.plain.strip(), "a bold b italic c both d")
        self.assertTrue(_has(content, "bold", "bold"))
        self.assertTrue(_has(content, "italic", "italic"))
        self.assertTrue(_has(content, "both", "bold"))
        self.assertTrue(_has(content, "both", "italic"))

    def test_underscore_emphasis_and_snake_case(self):
        content, _ = render_markdown("x __strong__ y _em_ z and my_var_name")
        self.assertTrue(_has(content, "strong", "bold"))
        self.assertTrue(_has(content, "em", "italic"))
        self.assertIn("my_var_name", content.plain)  # not mangled

    def test_inline_code_verbatim_and_styled(self):
        content, _ = render_markdown("run `pip install com2tty` now")
        self.assertIn("pip install com2tty", content.plain)
        self.assertNotIn("`", content.plain)
        self.assertTrue(_has(content, "pip install com2tty", "$panel"))

    def test_link_is_clickable_with_href_action(self):
        content, _ = render_markdown("see [the docs](#usage) please")
        self.assertIn("the docs", content.plain)
        self.assertNotIn("](", content.plain)
        self.assertEqual(_link_meta(content, "the docs"), "link('#usage')")

    def test_link_with_quote_in_href_is_plain(self):
        # A href that would break the action string is rendered as plain text.
        content, _ = render_markdown("[x](javascript:alert('x'))")
        self.assertIn("x", content.plain)
        self.assertIsNone(_link_meta(content, "x"))

    def test_image_shows_alt_text(self):
        content, _ = render_markdown("![a diagram](pic.png)")
        self.assertIn("a diagram", content.plain)
        self.assertNotIn("pic.png", content.plain)

    def test_backslash_escape(self):
        content, _ = render_markdown(r"literal \*stars\* here")
        self.assertIn("*stars*", content.plain)


class TestBlocks(unittest.TestCase):

    def test_atx_headings_and_anchors(self):
        content, anchors = render_markdown("# One\n## Two Words\n### Three")
        for word in ("One", "Two Words", "Three"):
            self.assertTrue(_has(content, word, "bold"))
        self.assertNotIn("#", content.plain)
        # GitHub-style slugs map to line indices.
        self.assertIn("one", anchors)
        self.assertIn("two-words", anchors)
        self.assertIn("three", anchors)
        self.assertEqual(anchors["one"], 0)

    def test_duplicate_heading_slugs_are_deduped(self):
        _, anchors = render_markdown("# Setup\n\ntext\n\n# Setup")
        self.assertIn("setup", anchors)
        self.assertIn("setup-1", anchors)

    def test_h1_gets_underline_rule(self):
        content, _ = render_markdown("# Title")
        self.assertIn("─", content.plain)

    def test_setext_headings(self):
        content, anchors = render_markdown("Big Title\n=====\n\nSub\n-----")
        self.assertTrue(_has(content, "Big Title", "bold"))
        self.assertTrue(_has(content, "Sub", "bold"))
        self.assertIn("big-title", anchors)
        self.assertNotIn("=====", content.plain)

    def test_fenced_code_is_verbatim_and_blocked(self):
        src = "```bash\nsudo ln -sf /tmp/ttyUSB0 /dev/ttyACM0\n```"
        content, _ = render_markdown(src)
        self.assertIn("sudo ln -sf /tmp/ttyUSB0 /dev/ttyACM0", content.plain)
        self.assertNotIn("```", content.plain)
        self.assertTrue(_has(content, "sudo ln", "$panel"))

    def test_fenced_code_lines_padded_to_block_width(self):
        # Short and long lines are padded to the same width (a neat block).
        content, _ = render_markdown("```\na\nlonger line\n```")
        lines = content.plain.split("\n")
        self.assertEqual(len(lines[0]), len(lines[1]))  # 'a' padded to width

    def test_fence_contents_not_inline_parsed(self):
        content, _ = render_markdown("```\na = **b** and `c`\n```")
        self.assertIn("a = **b** and `c`", content.plain)

    def test_unclosed_fence_still_renders(self):
        content, _ = render_markdown("```\nstuck open\nmore")
        self.assertIn("stuck open", content.plain)
        self.assertIn("more", content.plain)

    def test_unordered_list_bullets_and_nesting(self):
        content, _ = render_markdown("- first\n- second\n  - nested")
        self.assertIn("• first", content.plain)
        self.assertIn("  • nested", content.plain)
        self.assertNotIn("- first", content.plain)

    def test_ordered_list(self):
        content, _ = render_markdown("1. one\n2. two")
        self.assertIn("1. one", content.plain)
        self.assertIn("2. two", content.plain)

    def test_blockquote(self):
        content, _ = render_markdown("> quoted line")
        self.assertIn("quoted line", content.plain)
        self.assertTrue(_has(content, "quoted line", "italic"))
        self.assertIn("▌", content.plain)

    def test_horizontal_rule(self):
        content, _ = render_markdown("above\n\n---\n\nbelow")
        self.assertIn("─", content.plain)
        self.assertIn("above", content.plain)
        self.assertIn("below", content.plain)

    def test_pipe_table_aligned_header_bold(self):
        content, _ = render_markdown("| Name | Port |\n| --- | --- |\n| pico | COM3 |")
        self.assertIn("Name", content.plain)
        self.assertIn("pico", content.plain)
        self.assertNotIn("---", content.plain)
        self.assertTrue(_has(content, "Name", "bold"))

    def test_table_with_only_a_divider_renders_nothing(self):
        content, _ = render_markdown("| --- | --- |")
        self.assertEqual(content.plain.strip(), "")

    def test_empty_input(self):
        content, anchors = render_markdown("")
        self.assertEqual(content.plain, "")
        self.assertEqual(anchors, {})

    def test_real_readme_renders_with_anchors(self):
        path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        with open(path, encoding="utf-8") as handle:
            content, anchors = render_markdown(handle.read())
        self.assertGreater(len(content.plain), 1000)
        self.assertNotIn("```", content.plain)
        # The table-of-contents targets resolve to real anchors.
        self.assertIn("dashboard-mode", anchors)
        self.assertIn("requirements", anchors)


if __name__ == "__main__":
    unittest.main()
