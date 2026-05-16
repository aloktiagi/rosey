"""Telegram-native onboarding FSM.

Two coexisting flows, picked by the ``/start`` payload:

* ``/start signup`` (the QR target) drives the **v2 multi-step flow**:
  household name → members → timezone → upfront context → email → provisioning.
* Anything else (``/start`` alone, or any unknown DM) drives the older
  **v1 single-question flow**: name or invite code → provisioning. Useful
  for testers and direct-DM signups.

Either flow can short-circuit at any point if the user pastes a
``ROSEY-XXXX`` invite code — that always tries to redeem.

States (one per Telegram chat_id, prefixed ``tg:`` in
``onboarding_sessions``):

  v1: AWAITING_NAME_OR_CODE
  v2: AWAITING_HOUSEHOLD_NAME, AWAITING_MEMBERS, AWAITING_TIMEZONE,
      AWAITING_CONTEXT_OPT, AWAITING_EMAIL_OPT
  both: PROVISIONING
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from typing import Optional

from . import db, members as members_mod, timezone as timezone_mod

log = logging.getLogger(__name__)


SOFT_CAP = 25  # max concurrent active households on the free tier


@dataclass
class Reply:
    """Bot reply emitted by the FSM. Caller (router/app.py) sends ``text``
    to the user and, if ``trigger_provisioning``, kicks off the
    provisioning thread.
    """
    text: str
    trigger_provisioning: bool = False


INVITE_CODE_RE = re.compile(r"^ROSEY-[A-Z0-9]{4}$")
NAME_MIN, NAME_MAX = 1, 80
CONTEXT_MAX = 500
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Tokens that count as "skip" for the optional questions (context, email).
_OPT_SKIP_TOKENS = {"skip", "no", "none", "n/a", "nope", "pass", "no thanks"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def handle(
    engine,
    chat_id: int,
    sender_first_name: str,
    text: str,
    payload: str = "",
) -> Reply:
    """Drive one Telegram onboarding turn for an unknown sender.

    ``payload`` is the ``/start <payload>`` argument extracted by the
    router (empty string for a plain ``/start`` or any other message).
    """
    sender_id = f"tg:{chat_id}"
    text = (text or "").strip()

    # Invite codes redeem from any state.
    if INVITE_CODE_RE.match(text.upper()):
        return _try_redeem(engine, sender_id, sender_first_name, text.upper())

    sess = db.get_onboarding(engine, sender_id)

    if sess is None:
        if db.household_count(engine) >= SOFT_CAP:
            return Reply(
                "Rosey is at capacity for free signups right now. "
                "We'll let you know when we open more spots. 🙏"
            )
        if payload == "signup":
            return _start_v2(engine, sender_id, sender_first_name)
        return _start_v1(engine, sender_id, sender_first_name)

    state = sess["state"]

    # v1 flow
    if state == "AWAITING_NAME_OR_CODE":
        return _accept_name_v1(engine, sender_id, text)

    # v2 flow
    if state == "AWAITING_HOUSEHOLD_NAME":
        return _accept_household_name(engine, sender_id, sess, sender_first_name, text)
    if state == "AWAITING_MEMBERS":
        return _accept_members(engine, sender_id, sess, text)
    if state == "AWAITING_TIMEZONE":
        return _accept_timezone(engine, sender_id, sess, text)
    if state == "AWAITING_CONTEXT_OPT":
        return _accept_context(engine, sender_id, sess, text)
    if state == "AWAITING_EMAIL_OPT":
        return _accept_email(engine, sender_id, sess, text)

    if state == "PROVISIONING":
        return Reply(
            "Still setting up your household — I'll ping you when it's ready 🙂"
        )

    log.warning("unknown telegram onboarding state %r for %s", state, sender_id)
    return Reply("Hmm, something's off — try /start.")


# ---------------------------------------------------------------------------
# v1 flow: name OR invite code → provision
# ---------------------------------------------------------------------------

def _start_v1(engine, sender_id: str, sender_first_name: str) -> Reply:
    db.upsert_onboarding(engine, sender_id, "AWAITING_NAME_OR_CODE", {})
    return Reply(
        f"👋 Hi {sender_first_name or 'there'}! I'm Rosey — a shared "
        "assistant for your household.\n\n"
        "If you're starting a new household, reply with your name.\n"
        "If someone in your family sent you an invite code, paste it "
        "here (looks like ROSEY-A1B2)."
    )


def _accept_name_v1(engine, sender_id: str, text: str) -> Reply:
    name = text.strip()
    if not _looks_like_name(name):
        return Reply("I didn't catch that — what should I call you? (just a name)")

    db.upsert_onboarding(
        engine,
        sender_id,
        "PROVISIONING",
        {"flow": "v1", "admin_name": name, "members": []},
    )
    return Reply(
        f"Setting up your household, {name}! Give me ~60 seconds. 🛠️\n\n"
        "When ready, you can invite family with /invite <their name>.",
        trigger_provisioning=True,
    )


# ---------------------------------------------------------------------------
# v2 flow: household name → members → tz → context → email → provision
# ---------------------------------------------------------------------------

def _start_v2(engine, sender_id: str, sender_first_name: str) -> Reply:
    db.upsert_onboarding(
        engine,
        sender_id,
        "AWAITING_HOUSEHOLD_NAME",
        {"flow": "v2", "admin_name": sender_first_name or ""},
    )
    return Reply(
        f"👋 Hi {sender_first_name or 'there'}! I'm Rosey — a shared "
        "assistant for your household.\n\n"
        "Quick setup, takes about a minute.\n\n"
        "**Q1 of 5:** What's your household called? "
        "(e.g. \"The Tandons\", \"Apartment 4B\", \"Smith family\")"
    )


def _accept_household_name(
    engine, sender_id: str, sess: dict, sender_first_name: str, text: str,
) -> Reply:
    name = text.strip()
    if not _looks_like_name(name):
        return Reply(
            "I didn't catch that — what should I call your household? "
            "A short name like \"The Tandons\" is perfect."
        )

    data = sess["data"]
    data["household_name"] = name
    # Make sure admin_name is set — Telegram doesn't always supply first_name
    if not data.get("admin_name"):
        data["admin_name"] = sender_first_name or "Admin"
    db.upsert_onboarding(engine, sender_id, "AWAITING_MEMBERS", data)
    return Reply(
        f"Got it — **{name}**.\n\n"
        "**Q2 of 5:** Who else is in your family? List them one per line, "
        "with their Telegram username if you know it:\n\n"
        "```\n"
        "Sarah @sarah_t\n"
        "Mom @lakshmi_tandon\n"
        "Dad\n"
        "```\n\n"
        "If it's just you for now, reply \"skip\"."
    )


def _accept_members(engine, sender_id: str, sess: dict, text: str) -> Reply:
    parsed = members_mod.parse_members(text)
    data = sess["data"]
    data["members"] = [
        {"name": m.name, "tg_username": m.tg_username} for m in parsed
    ]
    db.upsert_onboarding(engine, sender_id, "AWAITING_TIMEZONE", data)
    count = len(parsed)
    summary = (
        "Great, just you for now."
        if count == 0
        else f"Got {count} other " + ("person" if count == 1 else "people") + "."
    )
    return Reply(
        f"{summary}\n\n"
        "**Q3 of 5:** What timezone are you in? "
        "A city or abbreviation works — \"San Francisco\", \"PST\", "
        "\"Mumbai\", \"UTC+1\"."
    )


def _accept_timezone(engine, sender_id: str, sess: dict, text: str) -> Reply:
    iana = timezone_mod.resolve(text)
    data = sess["data"]
    if iana is None:
        attempts = int(data.get("tz_attempts", 0)) + 1
        data["tz_attempts"] = attempts
        if attempts >= 2:
            # Fall through with UTC and tell them they can fix it later.
            data["timezone"] = "UTC"
            db.upsert_onboarding(engine, sender_id, "AWAITING_CONTEXT_OPT", data)
            return Reply(
                "I couldn't match that to a timezone — I'll use UTC for now. "
                "You can change it anytime by telling Rosey "
                "\"set our timezone to <city>\".\n\n"
                "**Q4 of 5:** Anything I should know about your household upfront? "
                "(kids, pets, schedules, allergies, who works late — anything helps. "
                "Or reply \"skip\".)"
            )
        db.upsert_onboarding(engine, sender_id, "AWAITING_TIMEZONE", data)
        return Reply(
            "Hmm, I didn't recognize that timezone. Try a city "
            "(\"San Francisco\", \"London\", \"Mumbai\") or an abbreviation "
            "(\"PST\", \"GMT\", \"IST\")."
        )

    data["timezone"] = iana
    db.upsert_onboarding(engine, sender_id, "AWAITING_CONTEXT_OPT", data)
    return Reply(
        f"Locked in: **{iana}**.\n\n"
        "**Q4 of 5:** Anything I should know about your household upfront? "
        "(kids, pets, schedules, allergies, who works late — anything helps. "
        "Or reply \"skip\".)"
    )


def _accept_context(engine, sender_id: str, sess: dict, text: str) -> Reply:
    data = sess["data"]
    if _is_opt_skip(text):
        data["upfront_context"] = ""
    else:
        data["upfront_context"] = text[:CONTEXT_MAX].strip()
    db.upsert_onboarding(engine, sender_id, "AWAITING_EMAIL_OPT", data)
    return Reply(
        "Noted.\n\n"
        "**Q5 of 5:** Want a welcome guide emailed to you? "
        "Drop an email address, or reply \"skip\"."
    )


def _accept_email(engine, sender_id: str, sess: dict, text: str) -> Reply:
    data = sess["data"]
    if _is_opt_skip(text):
        data["email"] = ""
    elif EMAIL_RE.match(text.strip()):
        data["email"] = text.strip().lower()
    else:
        return Reply(
            "That doesn't look like an email — try again, or reply \"skip\"."
        )

    db.upsert_onboarding(engine, sender_id, "PROVISIONING", data)
    return Reply(
        f"Setting up **{data.get('household_name', 'your household')}** — "
        "give me about 30 seconds. 🛠️\n\n"
        "Once it's ready, I'll send you a button to create your family group.",
        trigger_provisioning=True,
    )


# ---------------------------------------------------------------------------
# Invite-code redemption (shared)
# ---------------------------------------------------------------------------

def _try_redeem(engine, sender_id: str, sender_first_name: str, code: str) -> Reply:
    invite = db.lookup_invite_code(engine, code)
    if invite is None:
        return Reply(
            f"That code ({code}) isn't valid, has expired, or has already been used."
        )

    name = (invite.get("invitee_name") or sender_first_name or "Friend").strip()
    db.add_member(engine, sender_id, invite["household_id"], name)
    db.mark_invite_used(engine, code, sender_id)
    db.delete_onboarding(engine, sender_id)

    log.info(
        "invite code redeemed: %s by %s into household %s",
        code, sender_id, invite["household_id"],
    )
    return Reply(
        f"🎉 Welcome, {name}! You're now part of the household. "
        "Try 'add bananas to the list' or 'remind me Friday at 9am to take out the trash'."
    )


def generate_invite_code(engine, member: dict, invitee_name: str) -> str:
    """Generate and persist an invite code for an existing member's household.

    Caller (router/app.py /invite intercept, and provisioning.py at signup
    completion) must have already verified the household exists.
    """
    code = "ROSEY-" + secrets.token_hex(2).upper()  # 4 hex chars
    db.create_invite_code(
        engine,
        code=code,
        household_id=member["household_id"],
        # The "phone" column actually stores any identifier (tg:NNN). For
        # v2 onboarding we sometimes generate codes before the admin row
        # is final; callers may pass a synthetic "system" identifier.
        created_by=member.get("phone", "system"),
        invitee_name=invitee_name,
    )
    return code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _looks_like_name(text: str) -> bool:
    return (
        NAME_MIN <= len(text) <= NAME_MAX
        and any(c.isalpha() for c in text)
    )


def _is_opt_skip(text: str) -> bool:
    return (text or "").strip().lower() in _OPT_SKIP_TOKENS
