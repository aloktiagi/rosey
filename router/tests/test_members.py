"""Tests for router.members — the "who's in the family?" parser."""
from __future__ import annotations

import unittest

from router.members import Member, is_skip, parse_member_line, parse_members


class TestParseMemberLine(unittest.TestCase):
    def test_name_and_username(self):
        m = parse_member_line("Sarah @sarah_t")
        self.assertEqual(m, Member(name="Sarah", tg_username="sarah_t"))

    def test_just_name(self):
        m = parse_member_line("Mom")
        self.assertEqual(m, Member(name="Mom", tg_username=None))

    def test_just_username(self):
        m = parse_member_line("@sarah_t")
        # No display name supplied → fall back to username
        self.assertEqual(m, Member(name="sarah_t", tg_username="sarah_t"))

    def test_username_uppercases_lowered(self):
        m = parse_member_line("Ravi @Ravi_Kid_07")
        self.assertEqual(m.tg_username, "ravi_kid_07")

    def test_bullet_prefix_stripped(self):
        for prefix in ("- ", "* ", "• ", "  - "):
            m = parse_member_line(f"{prefix}Dad")
            self.assertEqual(m.name, "Dad", prefix)

    def test_extra_whitespace(self):
        m = parse_member_line("   Ravi   @ravi_kid_07   ")
        self.assertEqual(m, Member(name="Ravi", tg_username="ravi_kid_07"))

    def test_multi_word_name(self):
        m = parse_member_line("Mary Anne Smith @mary_anne")
        self.assertEqual(m, Member(name="Mary Anne Smith", tg_username="mary_anne"))

    def test_empty_line_returns_none(self):
        self.assertIsNone(parse_member_line(""))
        self.assertIsNone(parse_member_line("   "))
        self.assertIsNone(parse_member_line("\t"))

    def test_invalid_username_treated_as_name_fragment(self):
        # "@hi" is too short for a Telegram username — treat as text
        m = parse_member_line("Dad @hi")
        self.assertEqual(m.name, "Dad @hi")
        self.assertIsNone(m.tg_username)

    def test_name_at_max_length(self):
        name = "a" * 80
        m = parse_member_line(name)
        self.assertEqual(m.name, name)

    def test_name_over_max_length_rejected(self):
        self.assertIsNone(parse_member_line("a" * 81))


class TestParseMembers(unittest.TestCase):
    def test_typical_input(self):
        text = (
            "Sarah @sarah_t\n"
            "Mom @lakshmi_tandon\n"
            "Dad\n"
            "Ravi @ravi_kid_07\n"
        )
        members = parse_members(text)
        self.assertEqual(len(members), 4)
        self.assertEqual(members[0], Member(name="Sarah", tg_username="sarah_t"))
        self.assertEqual(members[2], Member(name="Dad", tg_username=None))

    def test_blank_lines_skipped(self):
        members = parse_members("Sarah @sarah_t\n\n\nMom\n")
        self.assertEqual(len(members), 2)

    def test_dedupe_by_username(self):
        # Same username on two lines — second is dropped
        members = parse_members("Sarah @sarah_t\nSarah-Other @sarah_t\n")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].name, "Sarah")

    def test_dedupe_by_name_case_insensitive(self):
        members = parse_members("Mom\nMOM\nmom @somethingelse\n")
        self.assertEqual(len(members), 1)

    def test_skip_token_returns_empty(self):
        for token in ("skip", "none", "just me", "JUST ME", "  No  "):
            self.assertEqual(parse_members(token), [], token)

    def test_skip_not_triggered_by_just_one_member(self):
        # "no" inside a longer answer shouldn't trigger skip
        members = parse_members("Nora @nora_b")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0].name, "Nora")

    def test_empty_input(self):
        self.assertEqual(parse_members(""), [])
        self.assertEqual(parse_members(None), [])  # type: ignore[arg-type]

    def test_mixed_format_robustness(self):
        text = (
            "- Sarah @sarah_t\n"
            "  *  Mom @LAKSHMI_T\n"
            "Dad\n"
            "@ravi_kid_07\n"
            "\n"
            "Aunt Sue\n"
        )
        members = parse_members(text)
        names = [m.name for m in members]
        self.assertIn("Sarah", names)
        self.assertIn("Mom", names)
        self.assertIn("Dad", names)
        self.assertIn("ravi_kid_07", names)
        self.assertIn("Aunt Sue", names)
        # Check username casing
        sarah = next(m for m in members if m.name == "Sarah")
        self.assertEqual(sarah.tg_username, "sarah_t")
        mom = next(m for m in members if m.name == "Mom")
        self.assertEqual(mom.tg_username, "lakshmi_t")


class TestIsSkip(unittest.TestCase):
    def test_obvious_skips(self):
        for s in ("skip", "Skip", "  SKIP  ", "none", "no one", "just me"):
            self.assertTrue(is_skip(s), s)

    def test_not_skip(self):
        for s in ("Sarah", "skip Sarah", "no really", "", None):
            self.assertFalse(is_skip(s), s)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
