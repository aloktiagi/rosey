"""Tests for the top-level ``household.py`` renderer.

These live under router/tests/ for convenience (single pytest invocation)
but exercise the household-VM code, not router code. The renderer must
accept all three identifier shapes that have appeared over the codebase:

  - ``telegram_id = "NNN"`` (canonical, used by household.toml.example
    and the v2 provisioning emitter)
  - ``telegram_username = "sarah_t"`` (v2 pending members)
  - ``phone = "tg:NNN"`` (legacy router shape, kept for backwards compat)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Add repo root to sys.path so we can import the top-level household module
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import household  # noqa: E402


class TestMemberID(unittest.TestCase):
    def test_telegram_id_numeric(self):
        self.assertEqual(household._member_id({"telegram_id": "100"}), "tg:100")
        self.assertEqual(household._member_id({"telegram_id": 100}), "tg:100")

    def test_telegram_id_already_prefixed(self):
        # Tolerate misconfig where someone put the "tg:" prefix in too
        self.assertEqual(household._member_id({"telegram_id": "tg:100"}), "tg:100")

    def test_telegram_username(self):
        self.assertEqual(
            household._member_id({"telegram_username": "sarah_t"}),
            "@sarah_t",
        )
        self.assertEqual(
            household._member_id({"telegram_username": "@SARAH_T"}),
            "@sarah_t",
        )

    def test_legacy_phone(self):
        self.assertEqual(
            household._member_id({"phone": "tg:100"}),
            "tg:100",
        )
        self.assertEqual(
            household._member_id({"phone": "@sarah_t"}),
            "@sarah_t",
        )

    def test_no_identifier(self):
        self.assertEqual(household._member_id({"name": "Dad"}), "")


class TestRender(unittest.TestCase):
    def test_renders_v2_config(self):
        md = household.render({
            "household_name": "The Tandons",
            "shopping_cadence": "weekly",
            "upfront_context": "we have a dog and 2 kids",
            "members": [
                {"name": "Ankit", "telegram_id": "100"},
                {"name": "Sarah", "telegram_username": "sarah_t"},
                {"name": "Dad"},
            ],
        })
        self.assertIn("# The Tandons", md)
        self.assertIn("- Ankit (tg:100)", md)
        self.assertIn("- Sarah (@sarah_t)", md)
        self.assertIn("- Dad", md)
        self.assertIn("Shopping cadence: weekly", md)
        self.assertIn("## About", md)
        self.assertIn("we have a dog and 2 kids", md)

    def test_renders_legacy_config(self):
        md = household.render({
            "shopping_cadence": "weekly",
            "default_store": "Whole Foods",
            "members": [{"name": "Alex", "telegram_id": "123456789"}],
        })
        self.assertIn("# Household", md)
        self.assertIn("- Alex (tg:123456789)", md)
        self.assertIn("Default store: Whole Foods", md)

    def test_renders_router_phone_shape(self):
        # Legacy router shape (before this PR fixed the field name)
        md = household.render({
            "members": [{"name": "Alex", "phone": "tg:100"}],
        })
        self.assertIn("- Alex (tg:100)", md)


if __name__ == "__main__":
    unittest.main()
