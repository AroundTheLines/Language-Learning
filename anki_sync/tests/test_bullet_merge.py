"""
Tests for anki_sync.bullet_merge.

Run with:
    python -m unittest anki_sync.tests.test_bullet_merge

These cover the merger as a pure function — no AnkiConnect, no I/O.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from anki_sync.bullet_merge import merge, parse, render, union


class TestParse(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(dict(parse("")), {})
        self.assertEqual(dict(parse(None)), {})

    def test_single_section_html(self):
        text = "Context:<br>• Hello<br>• World"
        result = parse(text)
        self.assertEqual(list(result.keys()), ["Context:"])
        self.assertEqual(result["Context:"], ["Hello", "World"])

    def test_single_section_newlines(self):
        text = "Context:\n• Hello\n• World"
        result = parse(text)
        self.assertEqual(result["Context:"], ["Hello", "World"])

    def test_two_sections(self):
        text = "Context:<br>• A<br>• B<br><br>Highlighted forms:<br>• X<br>• Y"
        result = parse(text)
        self.assertEqual(list(result.keys()), ["Context:", "Highlighted forms:"])
        self.assertEqual(result["Context:"], ["A", "B"])
        self.assertEqual(result["Highlighted forms:"], ["X", "Y"])

    def test_html_tags_stripped(self):
        text = "<div>Context:</div><br>• <i>Hello</i><br>• World"
        result = parse(text)
        self.assertEqual(result["Context:"], ["Hello", "World"])

    def test_div_wrapped_lines(self):
        # Anki sometimes wraps each line in <div>. Should still parse cleanly.
        text = "<div>Context:</div><div>• A</div><div>• B</div>"
        result = parse(text)
        self.assertEqual(result["Context:"], ["A", "B"])

    def test_html_entities_decoded(self):
        text = "Context:<br>• Hello&nbsp;world<br>• A&amp;B"
        result = parse(text)
        self.assertEqual(result["Context:"], ["Hello world", "A&B"])

    def test_orphan_bullets_dropped(self):
        # Bullets before any header have nowhere to go.
        text = "• stray<br>Context:<br>• kept"
        result = parse(text)
        self.assertEqual(result["Context:"], ["kept"])


class TestUnion(unittest.TestCase):
    def test_punctuation_dedup(self):
        existing = parse("Context:<br>• Asusta.")
        new = parse("Context:<br>• Asusta")
        result = union(existing, new)
        self.assertEqual(result["Context:"], ["Asusta."])  # original preserved

    def test_case_dedup(self):
        existing = parse("Context:<br>• Hola Mundo")
        new = parse("Context:<br>• hola mundo")
        result = union(existing, new)
        self.assertEqual(result["Context:"], ["Hola Mundo"])

    def test_whitespace_dedup(self):
        existing = parse("Context:<br>• Hola  mundo")
        new = parse("Context:<br>• Hola mundo")
        result = union(existing, new)
        self.assertEqual(len(result["Context:"]), 1)

    def test_unicode_normalization_dedup(self):
        # NFC vs NFD form of "está" should dedupe.
        existing = parse("Context:<br>• está")
        # NFD form: 'a' + combining acute
        nfd = "Context:<br>• esta\u0301"
        new = parse(nfd)
        result = union(existing, new)
        self.assertEqual(len(result["Context:"]), 1)

    def test_existing_preserved_first(self):
        existing = parse("Context:<br>• A<br>• B")
        new = parse("Context:<br>• C<br>• A")
        result = union(existing, new)
        self.assertEqual(result["Context:"], ["A", "B", "C"])

    def test_new_section_appended(self):
        existing = parse("Context:<br>• A")
        new = parse("Highlighted forms:<br>• X")
        result = union(existing, new)
        self.assertEqual(list(result.keys()), ["Context:", "Highlighted forms:"])

    def test_empty_existing(self):
        result = union(parse(""), parse("Context:<br>• A"))
        self.assertEqual(result["Context:"], ["A"])


class TestRender(unittest.TestCase):
    def test_basic_render(self):
        sections = parse("Context:<br>• A<br>• B")
        out = render(sections)
        self.assertEqual(out, "Context:<br>• A<br>• B")

    def test_two_section_render(self):
        sections = parse("Context:<br>• A<br><br>Highlighted forms:<br>• X")
        out = render(sections)
        self.assertEqual(out, "Context:<br>• A<br><br>Highlighted forms:<br>• X")

    def test_empty_sections_skipped(self):
        sections = parse("Context:<br>• A")
        sections["Empty:"] = []  # add an empty section
        out = render(sections)
        self.assertNotIn("Empty:", out)

    def test_round_trip_separator_change(self):
        sections = parse("Context:<br>• A")
        out = render(sections, separator="\n")
        self.assertEqual(out, "Context:\n• A")


class TestMerge(unittest.TestCase):
    def test_idempotent(self):
        text = "Context:<br>• A<br>• B<br><br>Highlighted forms:<br>• X"
        once = merge(text, text)
        twice = merge(once, once)
        self.assertEqual(once, twice)

    def test_real_world_cross_book(self):
        # Asustar from Percy Jackson, then re-encountered in book 2.
        existing = (
            "Context:<br>"
            "• Asusta. La mayor parte del tiempo sólo sirve para que te maten.<br>"
            "• ¿Te asustó algo?<br><br>"
            "Highlighted forms:<br>"
            "• Asusta.<br>"
            "• asustó"
        )
        new = (
            "Context:<br>"
            "• La sombra lo asustó de nuevo.<br>"
            "• ¿Te asustó algo?<br><br>"
            "Highlighted forms:<br>"
            "• asustó"
        )
        result = merge(existing, new)
        # 3 distinct contexts (Asusta, ¿Te asustó, La sombra) + 2 distinct forms (Asusta., asustó).
        self.assertEqual(result.count("• "), 5)
        self.assertEqual(result.count("¿Te asustó algo"), 1)
        # Existing order preserved.
        self.assertLess(result.find("Asusta. La mayor"), result.find("La sombra"))

    def test_first_creation(self):
        # Empty existing field: just emit the new content cleanly.
        new = "Context:<br>• Hola<br><br>Highlighted forms:<br>• Hola"
        result = merge("", new)
        self.assertIn("Context:", result)
        self.assertIn("• Hola", result)

    def test_no_change(self):
        existing = "Context:<br>• A"
        new = "Context:<br>• A"
        self.assertEqual(merge(existing, new), existing)

    def test_section_order_stable(self):
        # If existing has [Context, Highlighted forms], new sections should
        # not reorder them.
        existing = "Context:<br>• A<br><br>Highlighted forms:<br>• X"
        new = "Highlighted forms:<br>• Y<br><br>Context:<br>• B"
        result = merge(existing, new)
        self.assertLess(result.find("Context:"), result.find("Highlighted forms:"))


if __name__ == "__main__":
    unittest.main()
