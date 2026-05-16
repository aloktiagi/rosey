"""Tests for router.db — schema, helpers, and migrations.

Uses an in-memory SQLite DB per test for isolation.
"""
from __future__ import annotations

import unittest

from sqlalchemy import text

from router import db


def _engine():
    """Fresh in-memory SQLite engine per test, fully initialized."""
    e = db.get_engine("sqlite:///:memory:")
    db.init_db(e)
    return e


class TestSchema(unittest.TestCase):
    def test_init_db_creates_tables(self):
        e = _engine()
        with e.begin() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).fetchall()
            }
        for name in ("households", "members", "onboarding_sessions", "invite_codes"):
            self.assertIn(name, tables)

    def test_new_columns_present(self):
        e = _engine()
        with e.begin() as conn:
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(households)")).fetchall()
            }
            self.assertIn("group_chat_id", cols)
            mcols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(members)")).fetchall()
            }
            self.assertIn("tg_username", mcols)
            self.assertIn("email", mcols)

    def test_init_db_is_idempotent(self):
        e = _engine()
        # Second call must not raise
        db.init_db(e)
        db.init_db(e)


class TestMigrations(unittest.TestCase):
    def test_migrates_legacy_db(self):
        """Simulate a router DB created before the new columns existed,
        confirm init_db backfills them."""
        e = db.get_engine("sqlite:///:memory:")
        # Hand-create the OLD shape
        with e.begin() as conn:
            conn.execute(text(
                "CREATE TABLE households ("
                "id TEXT PRIMARY KEY, fly_app_name TEXT NOT NULL UNIQUE, "
                "status TEXT NOT NULL, "
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))
            conn.execute(text(
                "CREATE TABLE members ("
                "phone TEXT PRIMARY KEY, household_id TEXT NOT NULL, "
                "name TEXT NOT NULL, "
                "joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            ))
            # Seed one row of legacy data
            conn.execute(text(
                "INSERT INTO households (id, fly_app_name, status) "
                "VALUES ('h1', 'rosey-h-legacy', 'active')"
            ))
            conn.execute(text(
                "INSERT INTO members (phone, household_id, name) "
                "VALUES ('tg:42', 'h1', 'Legacy User')"
            ))

        db.init_db(e)  # should ALTER + add columns + indexes

        with e.begin() as conn:
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(households)")).fetchall()
            }
            self.assertIn("group_chat_id", cols)
            mcols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(members)")).fetchall()
            }
            self.assertIn("tg_username", mcols)
            self.assertIn("email", mcols)

        # And legacy data still intact
        h = db.lookup_household(e, "tg:42")
        self.assertIsNotNone(h)
        self.assertEqual(h["name"], "Legacy User")


class TestMemberHelpers(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()
        self.hid = db.create_household(self.engine, "rosey-h-test")

    def test_add_and_lookup_member(self):
        db.add_member(
            self.engine, "tg:100", self.hid, "Sarah",
            tg_username="Sarah_T", email="s@example.com",
        )
        row = db.lookup_household(self.engine, "tg:100")
        self.assertEqual(row["name"], "Sarah")
        self.assertEqual(row["household_id"], self.hid)

    def test_tg_username_lowercased_on_insert(self):
        db.add_member(self.engine, "tg:101", self.hid, "Sarah", tg_username="SARAH_T")
        with self.engine.begin() as conn:
            uname = conn.execute(
                text("SELECT tg_username FROM members WHERE phone = 'tg:101'")
            ).scalar()
        self.assertEqual(uname, "sarah_t")

    def test_add_pending_member(self):
        placeholder = db.add_pending_member(
            self.engine, self.hid, "Mom", tg_username="mom_t"
        )
        self.assertTrue(placeholder.startswith("pending:"))
        # Sanity: the row exists
        h = db.lookup_household(self.engine, placeholder)
        self.assertEqual(h["name"], "Mom")

    def test_lookup_pending_by_username(self):
        db.add_pending_member(self.engine, self.hid, "Mom", tg_username="mom_t")
        hit = db.lookup_pending_by_username(self.engine, "mom_t")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["name"], "Mom")
        self.assertEqual(hit["household_id"], self.hid)

    def test_lookup_pending_case_insensitive(self):
        db.add_pending_member(self.engine, self.hid, "Mom", tg_username="mom_t")
        self.assertIsNotNone(db.lookup_pending_by_username(self.engine, "MOM_T"))
        self.assertIsNotNone(db.lookup_pending_by_username(self.engine, "Mom_T"))

    def test_lookup_pending_misses_active(self):
        db.add_member(
            self.engine, "tg:200", self.hid, "Sarah", tg_username="sarah_t"
        )
        # tg_username is set, but this is an ACTIVE row (phone='tg:200')
        # so the pending lookup should NOT find it
        self.assertIsNone(db.lookup_pending_by_username(self.engine, "sarah_t"))

    def test_lookup_pending_ambiguous_returns_none(self):
        # Two pending rows with the same username across different households —
        # ambiguous match, caller falls back to onboarding.
        hid2 = db.create_household(self.engine, "rosey-h-other")
        db.add_pending_member(self.engine, self.hid, "Sarah", tg_username="sarah_t")
        db.add_pending_member(self.engine, hid2, "Other Sarah", tg_username="sarah_t")
        self.assertIsNone(db.lookup_pending_by_username(self.engine, "sarah_t"))

    def test_upgrade_pending_member(self):
        placeholder = db.add_pending_member(
            self.engine, self.hid, "Mom", tg_username="mom_t"
        )
        db.upgrade_pending_member(self.engine, placeholder, "tg:500")
        # Old phone is gone
        self.assertIsNone(db.lookup_household(self.engine, placeholder))
        # New phone resolves
        h = db.lookup_household(self.engine, "tg:500")
        self.assertEqual(h["name"], "Mom")
        # And the pending lookup should no longer find it (phone changed)
        self.assertIsNone(db.lookup_pending_by_username(self.engine, "mom_t"))


class TestGroupHelpers(unittest.TestCase):
    def setUp(self):
        self.engine = _engine()
        self.hid = db.create_household(self.engine, "rosey-h-test")
        db.add_member(self.engine, "tg:100", self.hid, "Sarah")

    def test_set_and_lookup_group(self):
        db.set_group_chat_id(self.engine, self.hid, -1001234567890)
        h = db.lookup_household_by_group(self.engine, -1001234567890)
        self.assertIsNotNone(h)
        self.assertEqual(h["id"], self.hid)

    def test_lookup_group_misses_unknown(self):
        self.assertIsNone(db.lookup_household_by_group(self.engine, -999))

    def test_lookup_household_returns_group_chat_id(self):
        db.set_group_chat_id(self.engine, self.hid, -1001234567890)
        h = db.lookup_household(self.engine, "tg:100")
        self.assertEqual(h["group_chat_id"], "-1001234567890")


if __name__ == "__main__":
    unittest.main()
