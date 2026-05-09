"""Outbound message dispatch by identifier prefix.

Identifier scheme:
    tg:NNN          → Telegram chat_id
    wa:+E164        → WhatsApp phone number (e.g. wa:+15551234567)

A note on rich text: Telegram supports `parse_mode="HTML"` (or
"MarkdownV2") and renders things like `<a href="tg://user?id=NNN">N</a>`
mention links. WhatsApp's Cloud API has no equivalent — any HTML in the
body is shown literally. So a reminder body crafted for Telegram with
HTML mentions would render as gibberish in WhatsApp ("<a href=...">N</a>"
shown verbatim). We handle this by stripping HTML in `send_whatsapp`
before posting to Meta, so callers can send the same Telegram-flavored
body to multi-channel recipients without per-channel branching.

To add a new channel: implement `send_<channel>(target, body) -> ...`,
add a branch in `send_returning_msg_id()`, and teach `roster.py` to
recognize the prefix.

Two return-type variants:
  send(...) -> bool               legacy callers; True/False on success
  send_returning_msg_id(...)      returns the platform message_id, or
                                   None on failure. Needed by the
                                   reminder lifecycle for reply-to-bot
                                   ack lookup (Telegram only — WhatsApp
                                   ack works via natural-language path).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger("rosey.channels")

# Regex stripper for HTML tags. We don't need a full HTML parser — the
# bodies coming through here only ever contain the simple subset Telegram
# accepts (<a>, <b>, <i>, <code>, <pre>). A bracket-stripping regex plus
# entity-unescape is sufficient and keeps channels.py dependency-free.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

TELEGRAM_TEXT_LIMIT = 4096
WHATSAPP_TEXT_LIMIT = 4096
WHATSAPP_API_BASE = "https://graph.facebook.com/v25.0"


def send(identifier: str, body: str, parse_mode: str | None = None) -> bool:
    """Dispatch outbound message by identifier prefix.

    Returns True on success, False if creds are missing or the API
    call failed. Errors are logged, not raised — fan-out callers should
    keep going for the other recipients.
    """
    return send_returning_msg_id(identifier, body, parse_mode=parse_mode) is not None


def send_returning_msg_id(
    identifier: str, body: str, parse_mode: str | None = None,
) -> int | str | None:
    """Like send() but returns the platform message_id on success.

    Return type varies by channel:
      - Telegram: int (Telegram's numeric `message.message_id`)
      - WhatsApp: str  (Meta's wamid string, e.g. "wamid.HBgM...")
      - None     on failure or unknown channel

    `parse_mode` is Telegram-specific; ignored for other channels.
    """
    if identifier.startswith("tg:"):
        return send_telegram(identifier[len("tg:"):], body, parse_mode=parse_mode)
    if identifier.startswith("wa:"):
        # wa: identifiers are stored as `wa:+E164` — strip both the
        # `wa:` prefix AND the optional + before passing to Meta's API
        # (which expects raw E.164 digits in the `to` field).
        phone = identifier[len("wa:"):].lstrip("+")
        return send_whatsapp(phone, body)
    log.warning("unknown identifier scheme: %s", identifier)
    return None


def send_telegram(
    chat_id: str, body: str, parse_mode: str | None = None,
) -> int | None:
    """Stateless POST to Telegram bot API. No SDK dependency.

    `chat_id` is the numeric ID as a string OR int (we coerce). Body is
    truncated to Telegram's 4096-char hard limit. Returns Telegram's
    `message.message_id` on 200, None otherwise.

    `parse_mode`, when set ("HTML" or "MarkdownV2"), enables Telegram's
    rich rendering. Most importantly: HTML lets us emit
    `<a href="tg://user?id=N">Name</a>` mention links that ping the user
    even when they don't have a public @username.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot send to tg:%s", chat_id)
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload_dict: dict = {
        "chat_id": int(chat_id),
        "text": body[:TELEGRAM_TEXT_LIMIT],
    }
    if parse_mode:
        payload_dict["parse_mode"] = parse_mode
    payload = json.dumps(payload_dict).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not data.get("ok"):
                log.warning("telegram sendMessage non-ok response: %s", data)
                return None
            return data.get("result", {}).get("message_id")
    except urllib.error.HTTPError as e:
        log.error(
            "telegram send failed to tg:%s status=%s body=%s",
            chat_id, e.code,
            e.read().decode("utf-8", errors="replace")[:300],
        )
        return None
    except Exception:
        log.exception("telegram send failed to tg:%s", chat_id)
        return None


def send_whatsapp(phone: str, body: str) -> str | None:
    """POST to Meta's WhatsApp Cloud API. Returns the wamid (message ID
    as a string) on success, None on failure.

    `phone` is E.164 *without* the leading `+` (Meta's API expectation).
    Caller is responsible for stripping the prefix; e.g. for a roster
    entry like `wa:+15551234567` the right call is
    `send_whatsapp("15551234567", body)`.

    Required environment:
      WHATSAPP_ACCESS_TOKEN      — long-lived or temporary EAA... token
      WHATSAPP_PHONE_NUMBER_ID   — the numeric ID of the sending phone
                                   number (NOT the phone number itself)

    Note on test mode: free-tier sends only deliver to recipients you've
    pre-verified in the Meta developer console, AND only within the
    24-hour conversation window after their last inbound message.
    Outside that window you'd need pre-approved templates. Reminders
    fired by the scheduler may not deliver via WhatsApp until you go
    through Meta's template approval flow.
    """
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_number_id:
        log.warning(
            "whatsapp creds missing — set WHATSAPP_ACCESS_TOKEN and "
            "WHATSAPP_PHONE_NUMBER_ID; cannot send to wa:%s", phone,
        )
        return None

    # Strip Telegram-flavored HTML before sending — WhatsApp displays
    # `<a href=...">N</a>` etc. literally, which looks like garbled
    # markup to the recipient. After stripping tags we also unescape
    # HTML entities so `&amp;` becomes `&`, `&lt;` becomes `<`, etc.
    plain_body = html.unescape(_HTML_TAG_RE.sub("", body))
    url = f"{WHATSAPP_API_BASE}/{phone_number_id}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": plain_body[:WHATSAPP_TEXT_LIMIT]},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            messages = data.get("messages") or []
            if messages:
                return messages[0].get("id")
            log.warning("whatsapp send to %s returned no messages: %s", phone, data)
            return None
    except urllib.error.HTTPError as e:
        log.error(
            "whatsapp send failed to wa:%s status=%s body=%s",
            phone, e.code,
            e.read().decode("utf-8", errors="replace")[:400],
        )
        return None
    except Exception:
        log.exception("whatsapp send failed to wa:%s", phone)
        return None
