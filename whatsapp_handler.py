"""WhatsApp Business Cloud API webhook handler.

Two HTTP methods on /whatsapp:

  GET  /whatsapp  — Meta's verification handshake during webhook setup.
                    Meta hits us with ?hub.mode=subscribe&hub.verify_token=...
                    &hub.challenge=...; we echo back the challenge if the
                    token matches WHATSAPP_VERIFY_TOKEN.

  POST /whatsapp  — incoming message events. Meta's envelope is nested
                    (entry → changes → value → messages). We walk down
                    to extract the actual user message and dispatch it
                    through the existing agent loop, reusing the same
                    trigger gating, fuzzy gate, ack mechanisms, and
                    persistent scheduler that Telegram uses.

Identifier scheme: senders are tagged `wa:+15551234567` (E.164 with +).
The roster (`household.md`) recognizes the prefix the same way it does
`tg:` — list members like `Ankit (wa:+15551234567)` and the agent will
attribute correctly.

Outbound replies go through `channels.send` which dispatches to
`channels.send_whatsapp` for `wa:` identifiers.

For test mode (free tier, up to 5 verified recipients) the only
consideration is the 24-hour customer-service window: we can reply to a
user freely within 24 hours of their last inbound message. After that,
proactive messages require pre-approved templates. Reminders fired by
the scheduler outside the 24h window won't deliver until you've gone
through Meta's template-approval flow — for now reminder reliability
stays better on Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os

from agent import handle_message

log = logging.getLogger("rosey.whatsapp")

VERIFY_TOKEN_ENV = "WHATSAPP_VERIFY_TOKEN"


def verify_webhook(mode: str, token: str, challenge: str) -> tuple[str, int]:
    """Handle Meta's GET handshake. Returns (response_body, http_status).

    Meta polls our endpoint once during webhook setup (and occasionally
    afterward) with the configured verify_token. We must respond with
    the `challenge` value verbatim and HTTP 200 if our shared secret
    matches; anything else and Meta refuses to register the webhook.
    """
    expected = os.environ.get(VERIFY_TOKEN_ENV)
    if not expected:
        log.error(
            "WHATSAPP_VERIFY_TOKEN not set — webhook verification cannot succeed. "
            "Set it via `fly secrets set WHATSAPP_VERIFY_TOKEN=<random>` and redeploy."
        )
        return "verify token not configured", 500
    if mode == "subscribe" and token == expected:
        log.info("whatsapp webhook verification accepted")
        return challenge, 200
    log.warning(
        "whatsapp webhook verification rejected — mode=%r token_match=%s",
        mode, token == expected,
    )
    return "forbidden", 403


async def handle_event(envelope: dict) -> None:
    """Process an inbound webhook event from Meta. May trigger outbound
    messages. The caller is expected to return 200 OK to Meta regardless
    of what happens here — Meta retries non-2xx responses, which we
    don't want for a transient agent error.
    """
    obj = envelope.get("object")
    if obj != "whatsapp_business_account":
        log.info("whatsapp: ignoring envelope object=%s", obj)
        return

    for entry in envelope.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            field = change.get("field")
            # We only process message events. Other fields (status updates,
            # template updates, etc.) come through the same webhook in
            # production but are noise for our use case.
            if field != "messages":
                log.info("whatsapp: skipping non-message field=%s", field)
                continue
            messages = value.get("messages") or []
            for msg in messages:
                try:
                    await _handle_message(msg, value)
                except Exception:
                    log.exception("whatsapp: handler crashed on message id=%s",
                                  msg.get("id", "?"))


async def _handle_message(msg: dict, value: dict) -> None:
    """Process a single inbound message. Routes through the existing
    agent loop and sends a reply via channels.send.
    """
    msg_type = msg.get("type")
    sender = msg.get("from", "")  # E.164 without leading +
    msg_id = msg.get("id", "")

    log.info("whatsapp: msg type=%s from=%s id=%s", msg_type, sender, msg_id)

    if msg_type != "text":
        # Non-text messages (image, audio, location, contact, etc.) are
        # out of scope for v1. Acknowledge politely instead of going silent.
        log.info("whatsapp: skipping non-text message type=%s", msg_type)
        from channels import send
        await asyncio.to_thread(
            send, f"wa:+{sender}",
            "I can only handle text messages right now. Try typing instead.",
        )
        return

    text = (msg.get("text") or {}).get("body", "").strip()
    if not text:
        return

    # Canonical identifier: prefix + plus-sign + E.164. Matches what users
    # would write in household.md (e.g. "Ankit (wa:+15551234567)").
    sender_id = f"wa:+{sender}"
    # 1:1 only in v1 — no group origin to fall back to. The from: tag in
    # any reminder lines the agent writes will point back to the speaker.
    origin_chat = sender_id

    try:
        reply = await asyncio.to_thread(
            handle_message, sender_id, text, origin_chat=origin_chat,
        )
    except Exception:
        log.exception("whatsapp: agent failure for %s", sender_id)
        reply = "Something went wrong. Try again in a moment."

    if reply:
        from channels import send
        await asyncio.to_thread(send, sender_id, reply)


# ---------------------------------------------------------------------------
# Baileys path — group-aware. The Cloud API path above can't reach groups
# (Meta gates that behind Official Business Account status), so the
# Baileys sidecar exists specifically to let Rosey participate in
# user-created WhatsApp groups. The shape of the inbound payload is
# whatever Node's index.js posts to /whatsapp-baileys, NOT Meta's
# nested entry/changes envelope.
# ---------------------------------------------------------------------------

async def handle_baileys_event(payload: dict) -> None:
    """Process an inbound message from the Baileys sidecar. Schema:
        {
          "message_id": "<wamid>",
          "sender_phone": "15551234567",     # E.164 without +
          "sender_jid":   "15551234567@s.whatsapp.net",
          "chat_jid":     "120363xx@g.us"   if group, else same as sender_jid,
          "is_group":     true|false,
          "text":         "the message body",
          "push_name":    "Sunanda" | null,
          "timestamp":    1731000000
        }
    """
    text = (payload.get("text") or "").strip()
    if not text:
        return
    sender_phone = payload.get("sender_phone") or ""
    chat_jid = payload.get("chat_jid") or ""
    is_group = bool(payload.get("is_group"))

    # Canonical sender identifier — the human who actually spoke.
    sender_id = f"wa:+{sender_phone}"

    # Where to reply: in a group, reply to the GROUP (so the conversation
    # stays in-thread); in a DM, reply to the speaker. Identifier
    # convention:
    #   wa:group:<JID>   → Baileys recognizes group destinations
    #   wa:+<phone>      → DM
    if is_group:
        reply_target = f"wa:group:{chat_jid}"
        # origin_chat for any reminders the agent writes points back to
        # this group, so future scheduler fan-outs that resolve via the
        # `from:` tag will fire into the group too.
        origin_chat = reply_target
    else:
        reply_target = sender_id
        origin_chat = sender_id

    log.info(
        "baileys: msg from=%s in=%s text_len=%d",
        sender_phone, "group" if is_group else "dm", len(text),
    )

    try:
        reply = await asyncio.to_thread(
            handle_message, sender_id, text, origin_chat=origin_chat,
        )
    except Exception:
        log.exception("baileys: agent failure for %s", sender_id)
        reply = "Something went wrong. Try again in a moment."

    # Empty reply = agent decided not to respond (fuzzy gate said NO,
    # or other intentional silence). Don't post anything.
    if not reply:
        return

    from channels import send
    await asyncio.to_thread(send, reply_target, reply)
