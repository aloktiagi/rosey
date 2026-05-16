"""Tests for router.provisioning — the v1 + v2 dispatch logic.

Always runs with ROUTER_DRY_RUN=1 so flyctl is never called. We verify:
  - DB rows committed correctly (households, members, invite_codes)
  - v2 pre-rosters members as pending
  - v2 generates one invite code per rostered member
  - v2 welcome is sent with the right inline-button URL
  - TOML rendering accepts the new optional fields
  - Secrets bundle includes SCHEDULER_TZ when a timezone is provided
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# Set BEFORE importing router.app's transitive deps.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ROUTER_DRY_RUN", "1")

from router import db, provisioning  # noqa: E402


def _engine():
    e = db.get_engine("sqlite:///:memory:")
    db.init_db(e)
    return e


class TestRenderHouseholdToml(unittest.TestCase):
    def test_v1_shape(self):
        toml = provisioning._render_household_toml(
            "Ankit", "tg:100",
            members=[{"name": "Sarah", "phone": "tg:200"}],
        )
        self.assertIn("shopping_cadence", toml)
        self.assertIn('name = "Ankit"', toml)
        # "tg:100" → telegram_id = "100" (canonical, matches household.py)
        self.assertIn('telegram_id = "100"', toml)
        self.assertIn('name = "Sarah"', toml)
        self.assertIn('telegram_id = "200"', toml)

    def test_v2_shape_with_household_name_and_context(self):
        toml = provisioning._render_household_toml(
            "Ankit", "tg:100",
            members=[
                {"name": "Sarah", "tg_username": "sarah_t"},
                {"name": "Dad"},  # no username, no id
            ],
            household_name="The Tandons",
            upfront_context="dog + 2 kids in school",
        )
        self.assertIn('household_name = "The Tandons"', toml)
        self.assertIn('upfront_context = "dog + 2 kids in school"', toml)
        self.assertIn('telegram_id = "100"', toml)  # admin
        self.assertIn('name = "Sarah"', toml)
        # v2 pending member with a username uses telegram_username
        self.assertIn('telegram_username = "sarah_t"', toml)
        # Member without any identifier still appears in the roster
        self.assertIn('name = "Dad"', toml)

    def test_special_characters_escaped(self):
        # Make sure quotes / backslashes in household_name don't break TOML
        toml = provisioning._render_household_toml(
            "Ankit", "tg:100", members=[],
            household_name='The "Tandons"',
        )
        self.assertIn(r'household_name = "The \"Tandons\""', toml)


class TestCollectSecrets(unittest.TestCase):
    @patch.dict(os.environ, {
        "ANTHROPIC_API_KEY": "x",
        "OPENAI_API_KEY": "y",
        "ROSEY_INTERNAL_TOKEN": "z",
        "TELEGRAM_BOT_TOKEN": "w",
    }, clear=False)
    def test_v1_no_timezone(self):
        secrets = provisioning._collect_secrets(
            "Ankit", "tg:100", members=[],
        )
        self.assertNotIn("SCHEDULER_TZ", secrets)
        self.assertIn("HOUSEHOLD_TOML", secrets)

    @patch.dict(os.environ, {
        "ANTHROPIC_API_KEY": "x",
        "OPENAI_API_KEY": "y",
        "ROSEY_INTERNAL_TOKEN": "z",
        "TELEGRAM_BOT_TOKEN": "w",
    }, clear=False)
    def test_v2_with_timezone(self):
        secrets = provisioning._collect_secrets(
            "Ankit", "tg:100", members=[],
            timezone="Asia/Kolkata",
        )
        self.assertEqual(secrets["SCHEDULER_TZ"], "Asia/Kolkata")

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_required_env_raises(self):
        with self.assertRaises(RuntimeError):
            provisioning._collect_secrets("Ankit", "tg:100", members=[])


class TestProvisionV2(unittest.TestCase):
    """End-to-end v2 _provision call, dry-run flyctl, with a real DB."""

    @patch("router.provisioning.notifications.send_with_url_button")
    @patch("router.provisioning.time.sleep")  # skip the dry-run delay
    def test_v2_full_flow(self, _sleep, mock_button):
        eng = _engine()
        # Seed the onboarding session — what the FSM would have written
        db.upsert_onboarding(eng, "tg:100", "PROVISIONING", {
            "flow": "v2",
            "admin_name": "Ankit",
            "household_name": "The Tandons",
            "members": [
                {"name": "Sarah", "tg_username": "sarah_t"},
                {"name": "Mom", "tg_username": "lakshmi_t"},
                {"name": "Dad", "tg_username": None},
            ],
            "timezone": "America/Los_Angeles",
            "upfront_context": "dog + 2 kids",
            "email": "ankit@example.com",
        })

        provisioning._provision(eng, "tg:100")

        # Household exists, status active
        with eng.begin() as conn:
            from sqlalchemy import text
            count = conn.execute(text("SELECT COUNT(*) FROM households")).scalar()
        self.assertEqual(count, 1)

        # Admin is an active member
        admin = db.lookup_household(eng, "tg:100")
        self.assertEqual(admin["name"], "Ankit")
        hid = admin["household_id"]

        # 3 pending members with usernames
        with eng.begin() as conn:
            from sqlalchemy import text
            rows = conn.execute(text(
                "SELECT name, tg_username, phone FROM members "
                "WHERE household_id = :hid AND phone LIKE 'pending:%'"
            ), {"hid": hid}).mappings().all()
        self.assertEqual(len(rows), 3)
        names = {r["name"] for r in rows}
        self.assertEqual(names, {"Sarah", "Mom", "Dad"})
        # Sarah and Mom have usernames; Dad doesn't
        usernames = {r["name"]: r["tg_username"] for r in rows}
        self.assertEqual(usernames["Sarah"], "sarah_t")
        self.assertEqual(usernames["Mom"], "lakshmi_t")
        self.assertIsNone(usernames["Dad"])

        # 3 invite codes generated, all tied to this household
        with eng.begin() as conn:
            from sqlalchemy import text
            codes = conn.execute(text(
                "SELECT code, invitee_name FROM invite_codes WHERE household_id = :hid"
            ), {"hid": hid}).mappings().all()
        self.assertEqual(len(codes), 3)
        invitee_names = {c["invitee_name"] for c in codes}
        self.assertEqual(invitee_names, {"Sarah", "Mom", "Dad"})

        # Onboarding session cleared
        self.assertIsNone(db.get_onboarding(eng, "tg:100"))

        # v2 welcome called with inline button
        mock_button.assert_called_once()
        kwargs = mock_button.call_args.kwargs
        self.assertEqual(kwargs["button_label"], "Create family group")
        self.assertIn("startgroup=ready", kwargs["button_url"])

    @patch("router.provisioning.notifications.send_text")
    @patch("router.provisioning.time.sleep")
    def test_v1_legacy_flow_unchanged(self, _sleep, mock_send):
        eng = _engine()
        db.upsert_onboarding(eng, "tg:200", "PROVISIONING", {
            "flow": "v1",
            "admin_name": "Solo User",
            "members": [],
        })

        provisioning._provision(eng, "tg:200")

        # Admin row, no pending, no codes
        admin = db.lookup_household(eng, "tg:200")
        self.assertEqual(admin["name"], "Solo User")
        with eng.begin() as conn:
            from sqlalchemy import text
            pending = conn.execute(text(
                "SELECT COUNT(*) FROM members WHERE phone LIKE 'pending:%'"
            )).scalar()
            codes = conn.execute(text("SELECT COUNT(*) FROM invite_codes")).scalar()
        self.assertEqual(pending, 0)
        self.assertEqual(codes, 0)
        # v1 uses simple welcome (send_text, not the button variant)
        mock_send.assert_called()


if __name__ == "__main__":
    unittest.main()
