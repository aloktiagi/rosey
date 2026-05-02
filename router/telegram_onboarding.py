"""Telegram-native onboarding FSM.

  - Admin signs up first as a single-member household.
  - Admin generates invite codes via /invite <name> from inside the bot.
  - Each invitee /starts the bot and pastes the code; the router adds
    them to the existing household.

New members self-register by interacting with the bot directly — no
chat_ids need to be collected up-front.

States (one PK per Telegram chat_id, prefixed `tg:` in the
`onboarding_sessions` table):

  AWAITING_NAME_OR_CODE  → first message after /start. Either a name
                           (start a new household) or an invite code
                           (join existing).
  PROVISIONING           → background task is creating the household VM.

Codes redeem from any state, so a half-onboarded admin can still join
someone else's household by pasting a code instead.
"""
from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass

from . import db

log = logging.getLogger(__name__)


SOFT_CAP = 25  # max concurrent active households on the free tier


@dataclass
class Reply:
    """Bot reply emitted by the FSM. Caller (router/app.py) sends `text` to
    the user and, if `trigger_provisioning`, kicks off the provisioning thread.
    """
    text: str
    trigger_provisioning: bool = False

INVITE_CODE_RE = re.compile(r"^ROSEY-[A-Z0-9]{4}$")
NAME_MIN, NAME_MAX = 1, 80


def handle(engine, chat_id: int, sender_first_name: str, text: str) -> Reply:
    """Drive a Telegram-native onboarding turn for an unknown sender."""
    sender_id = f"tg:{chat_id}"
    text = (text or "").strip()
    upper = text.upper()

    # Any message that looks like an invite code: try to redeem,
    # regardless of state.
    if INVITE_CODE_RE.match(upper):
        return _try_redeem(engine, sender_id, sender_first_name, upper)

    sess = db.get_onboarding(engine, sender_id)

    if sess is None:
        # First contact — gate at the soft cap, then prompt.
        if db.household_count(engine) >= SOFT_CAP:
            return Reply(
                "Rosey is at capacity for free signups right now. "
                "We'll let you know when we open more spots. 🙏"
            )
        db.upsert_onboarding(engine, sender_id, "AWAITING_NAME_OR_CODE", {})
        return Reply(
            f"👋 Hi {sender_first_name or 'there'}! I'm Rosey — a shared "
            "assistant for your household.\n\n"
            "If you're starting a new household, reply with your name.\n"
            "If someone in your family sent you an invite code, paste it "
            "here (looks like ROSEY-A1B2)."
        )

    state = sess["state"]
    if state == "AWAITING_NAME_OR_CODE":
        return _accept_name(engine, sender_id, text)
    if state == "PROVISIONING":
        return Reply(
            "Still setting up your household — I'll ping you when it's ready 🙂"
        )

    log.warning("unknown telegram onboarding state %r for %s", state, sender_id)
    return Reply("Hmm, something's off — try /start.")


def _accept_name(engine, sender_id: str, text: str) -> Reply:
    name = text.strip()
    if not (NAME_MIN <= len(name) <= NAME_MAX) or not any(c.isalpha() for c in name):
        return Reply("I didn't catch that — what should I call you? (just a name)")

    db.upsert_onboarding(
        engine,
        sender_id,
        "PROVISIONING",
        {"admin_name": name, "members": []},
    )
    return Reply(
        f"Setting up your household, {name}! Give me ~60 seconds. 🛠️\n\n"
        "When ready, you can invite family with /invite <their name>.",
        trigger_provisioning=True,
    )


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

    Caller (router/app.py /invite intercept) must verify the requesting
    user is a member of a household before calling this.
    """
    code = "ROSEY-" + secrets.token_hex(2).upper()  # 4 hex chars
    db.create_invite_code(
        engine,
        code=code,
        household_id=member["household_id"],
        created_by=member["phone"],  # the existing column name is "phone" but stores any identifier
        invitee_name=invitee_name,
    )
    return code
