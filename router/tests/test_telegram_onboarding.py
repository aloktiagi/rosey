"""Tests for the Telegram onboarding FSM.

Walks the new v2 6-state flow end-to-end with an in-memory router DB.
Also covers the legacy v1 flow, invite-code redemption, and edge cases
like timezone reattempts and abandoned sessions.
"""
from __future__ import annotations

import unittest

from router import db, telegram_onboarding as fsm


def _engine():
    e = db.get_engine("sqlite:///:memory:")
    db.init_db(e)
    return e


def _walk(engine, chat_id: int, *messages: tuple) -> list[fsm.Reply]:
    """Helper: feed each message through fsm.handle, collect replies.

    Each ``messages`` entry is ``(text, payload)`` or just ``text``.
    """
    out: list[fsm.Reply] = []
    for m in messages:
        if isinstance(m, tuple):
            text, payload = m
        else:
            text, payload = m, ""
        out.append(fsm.handle(engine, chat_id, "Ankit", text, payload=payload))
    return out


class TestV2HappyPath(unittest.TestCase):
    """A new signup from /start signup → all six questions → provisioning."""

    def test_full_flow(self):
        e = _engine()
        # Step 1: /start signup payload
        r0 = fsm.handle(e, 100, "Ankit", "/start", payload="signup")
        # /start text comes in as "/start", which doesn't match invite-code,
        # and the FSM creates the onboarding row + asks Q1.
        self.assertIn("household called", r0.text.lower())
        self.assertFalse(r0.trigger_provisioning)

        sess = db.get_onboarding(e, "tg:100")
        self.assertEqual(sess["state"], "AWAITING_HOUSEHOLD_NAME")
        self.assertEqual(sess["data"]["flow"], "v2")

        # Step 2: household name
        r1 = fsm.handle(e, 100, "Ankit", "The Tandons")
        self.assertIn("Who else is in your family", r1.text)
        sess = db.get_onboarding(e, "tg:100")
        self.assertEqual(sess["state"], "AWAITING_MEMBERS")
        self.assertEqual(sess["data"]["household_name"], "The Tandons")

        # Step 3: members
        r2 = fsm.handle(
            e, 100, "Ankit",
            "Sarah @sarah_t\nMom @lakshmi_tandon\nDad",
        )
        self.assertIn("timezone", r2.text.lower())
        sess = db.get_onboarding(e, "tg:100")
        self.assertEqual(sess["state"], "AWAITING_TIMEZONE")
        self.assertEqual(len(sess["data"]["members"]), 3)

        # Step 4: timezone
        r3 = fsm.handle(e, 100, "Ankit", "San Francisco")
        self.assertIn("America/Los_Angeles", r3.text)
        sess = db.get_onboarding(e, "tg:100")
        self.assertEqual(sess["state"], "AWAITING_CONTEXT_OPT")
        self.assertEqual(sess["data"]["timezone"], "America/Los_Angeles")

        # Step 5: context
        r4 = fsm.handle(e, 100, "Ankit", "We have a dog and 2 kids in school")
        self.assertIn("email", r4.text.lower())
        sess = db.get_onboarding(e, "tg:100")
        self.assertEqual(sess["state"], "AWAITING_EMAIL_OPT")
        self.assertIn("dog", sess["data"]["upfront_context"])

        # Step 6: email → triggers provisioning
        r5 = fsm.handle(e, 100, "Ankit", "ankit@example.com")
        self.assertTrue(r5.trigger_provisioning)
        sess = db.get_onboarding(e, "tg:100")
        self.assertEqual(sess["state"], "PROVISIONING")
        self.assertEqual(sess["data"]["email"], "ankit@example.com")

    def test_skip_optional_fields(self):
        e = _engine()
        fsm.handle(e, 200, "Ankit", "/start", payload="signup")
        fsm.handle(e, 200, "Ankit", "Tandons")
        fsm.handle(e, 200, "Ankit", "skip")           # no other members
        fsm.handle(e, 200, "Ankit", "PST")            # timezone
        fsm.handle(e, 200, "Ankit", "skip")           # context
        r = fsm.handle(e, 200, "Ankit", "skip")       # email
        self.assertTrue(r.trigger_provisioning)
        data = db.get_onboarding(e, "tg:200")["data"]
        self.assertEqual(data["members"], [])
        self.assertEqual(data["upfront_context"], "")
        self.assertEqual(data["email"], "")


class TestTimezoneReAsk(unittest.TestCase):
    def test_unparseable_tz_first_attempt_re_asks(self):
        e = _engine()
        fsm.handle(e, 300, "Ankit", "/start", payload="signup")
        fsm.handle(e, 300, "Ankit", "Tandons")
        fsm.handle(e, 300, "Ankit", "skip")
        r = fsm.handle(e, 300, "Ankit", "the moon")
        self.assertIn("didn't recognize", r.text)
        sess = db.get_onboarding(e, "tg:300")
        # State unchanged — still asking
        self.assertEqual(sess["state"], "AWAITING_TIMEZONE")
        self.assertEqual(sess["data"]["tz_attempts"], 1)

    def test_unparseable_tz_second_attempt_falls_back_to_utc(self):
        e = _engine()
        fsm.handle(e, 301, "Ankit", "/start", payload="signup")
        fsm.handle(e, 301, "Ankit", "Tandons")
        fsm.handle(e, 301, "Ankit", "skip")
        fsm.handle(e, 301, "Ankit", "the moon")
        r = fsm.handle(e, 301, "Ankit", "still garbage")
        self.assertIn("UTC", r.text)
        sess = db.get_onboarding(e, "tg:301")
        self.assertEqual(sess["state"], "AWAITING_CONTEXT_OPT")
        self.assertEqual(sess["data"]["timezone"], "UTC")


class TestEmailValidation(unittest.TestCase):
    def test_invalid_email_re_asks(self):
        e = _engine()
        # Walk to email step
        for msg in [("/start", "signup"), "Tandons", "skip", "PST", "skip"]:
            if isinstance(msg, tuple):
                fsm.handle(e, 400, "Ankit", msg[0], payload=msg[1])
            else:
                fsm.handle(e, 400, "Ankit", msg)
        r = fsm.handle(e, 400, "Ankit", "not an email")
        self.assertIn("doesn't look like an email", r.text)
        self.assertFalse(r.trigger_provisioning)
        sess = db.get_onboarding(e, "tg:400")
        self.assertEqual(sess["state"], "AWAITING_EMAIL_OPT")

    def test_valid_email_proceeds(self):
        e = _engine()
        for msg in [("/start", "signup"), "Tandons", "skip", "PST", "skip"]:
            if isinstance(msg, tuple):
                fsm.handle(e, 401, "Ankit", msg[0], payload=msg[1])
            else:
                fsm.handle(e, 401, "Ankit", msg)
        r = fsm.handle(e, 401, "Ankit", "Ankit@Example.COM")
        self.assertTrue(r.trigger_provisioning)
        sess = db.get_onboarding(e, "tg:401")
        self.assertEqual(sess["data"]["email"], "ankit@example.com")


class TestV1LegacyFlow(unittest.TestCase):
    def test_plain_start_keeps_legacy_flow(self):
        e = _engine()
        # No payload → legacy single-question flow
        r0 = fsm.handle(e, 500, "Ankit", "/start", payload="")
        self.assertIn("starting a new household", r0.text)
        sess = db.get_onboarding(e, "tg:500")
        self.assertEqual(sess["state"], "AWAITING_NAME_OR_CODE")

        r1 = fsm.handle(e, 500, "Ankit", "Ankit T")
        self.assertTrue(r1.trigger_provisioning)
        data = db.get_onboarding(e, "tg:500")["data"]
        self.assertEqual(data["flow"], "v1")
        self.assertEqual(data["admin_name"], "Ankit T")

    def test_legacy_invalid_name_re_asks(self):
        e = _engine()
        fsm.handle(e, 501, "Ankit", "/start", payload="")
        r = fsm.handle(e, 501, "Ankit", "12345")
        self.assertIn("what should I call you", r.text)


class TestInviteCodeRedemption(unittest.TestCase):
    def test_redeems_from_no_session(self):
        e = _engine()
        # Set up an existing household + invite code
        hid = db.create_household(e, "rosey-h-host")
        db.add_member(e, "tg:1", hid, "Host User")
        db.create_invite_code(e, "ROSEY-AAAA", hid, "tg:1", "Sarah")

        r = fsm.handle(e, 999, "Sarah", "ROSEY-AAAA", payload="")
        self.assertIn("Welcome, Sarah", r.text)
        # Sarah is now a member
        sarah = db.lookup_household(e, "tg:999")
        self.assertEqual(sarah["household_id"], hid)

    def test_redeems_mid_v2_flow(self):
        e = _engine()
        hid = db.create_household(e, "rosey-h-host")
        db.add_member(e, "tg:1", hid, "Host User")
        db.create_invite_code(e, "ROSEY-BBBB", hid, "tg:1", "Bob")

        # Start v2 onboarding, then redeem a code instead
        fsm.handle(e, 600, "Bob", "/start", payload="signup")
        # We're at AWAITING_HOUSEHOLD_NAME — pasting a code still works
        r = fsm.handle(e, 600, "Bob", "ROSEY-BBBB")
        self.assertIn("Welcome", r.text)
        # Onboarding row cleared
        self.assertIsNone(db.get_onboarding(e, "tg:600"))

    def test_invalid_code_rejected(self):
        e = _engine()
        r = fsm.handle(e, 999, "Sarah", "ROSEY-ZZZZ")
        self.assertIn("isn't valid", r.text)


class TestSoftCap(unittest.TestCase):
    def test_cap_blocks_new_signups(self):
        e = _engine()
        # Hit the cap
        for i in range(fsm.SOFT_CAP):
            db.create_household(e, f"rosey-h-{i:04d}")
        r = fsm.handle(e, 999, "Ankit", "/start", payload="signup")
        self.assertIn("capacity", r.text.lower())
        # No onboarding row created
        self.assertIsNone(db.get_onboarding(e, "tg:999"))


class TestResumeMidFlow(unittest.TestCase):
    def test_user_picks_up_where_they_left_off(self):
        e = _engine()
        fsm.handle(e, 700, "Ankit", "/start", payload="signup")
        fsm.handle(e, 700, "Ankit", "Tandons")
        # Simulate user disconnects → comes back later → sends something
        r = fsm.handle(e, 700, "Ankit", "Sarah @sarah_t")
        # Should advance from MEMBERS → TIMEZONE
        self.assertIn("timezone", r.text.lower())
        self.assertEqual(
            db.get_onboarding(e, "tg:700")["state"], "AWAITING_TIMEZONE"
        )


class TestProvisioningEcho(unittest.TestCase):
    def test_message_during_provisioning_acks_softly(self):
        e = _engine()
        db.upsert_onboarding(e, "tg:800", "PROVISIONING", {"flow": "v2"})
        r = fsm.handle(e, 800, "Ankit", "are we there yet?")
        self.assertIn("setting up", r.text.lower())
        self.assertFalse(r.trigger_provisioning)


if __name__ == "__main__":
    unittest.main()
