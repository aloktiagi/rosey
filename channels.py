"""Outbound message dispatch by identifier prefix.

Currently Telegram-only. Kept as a dispatch module so a fork can drop in
a new channel by:
  1. Adding `send_<channel>(target, body) -> bool` below.
  2. Adding a branch in `send()` for the new prefix.
  3. Updating `roster.py:members()` to recognize the prefix in
     household.md entries.

Identifier scheme:
    tg:NNN  → Telegram chat_id

Two return-type variants:
  send(...) -> bool               legacy callers; True/False on success
  send_returning_msg_id(...)      returns the platform message_id (for
                                   Telegram), or None on failure. Needed
                                   by the reminder lifecycle for
                                   reply-to-bot ack lookup.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("rosey.channels")

TELEGRAM_TEXT_LIMIT = 4096


def send(identifier: str, body: str) -> bool:
    """Dispatch outbound message by identifier prefix.

    Returns True on success, False if creds are missing or the API
    call failed. Errors are logged, not raised — fan-out callers should
    keep going for the other recipients.
    """
    return send_returning_msg_id(identifier, body) is not None


def send_returning_msg_id(identifier: str, body: str) -> int | None:
    """Like send() but returns the platform message_id on success.

    For tg: identifiers, this is Telegram's `message.message_id` from
    the sendMessage response. None on failure or unknown channel.
    """
    if identifier.startswith("tg:"):
        return send_telegram(identifier[len("tg:"):], body)
    log.warning("unknown identifier scheme: %s", identifier)
    return None


def send_telegram(chat_id: str, body: str) -> int | None:
    """Stateless POST to Telegram bot API. No SDK dependency.

    `chat_id` is the numeric ID as a string OR int (we coerce). Body is
    truncated to Telegram's 4096-char hard limit. Returns Telegram's
    `message.message_id` on 200, None otherwise.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot send to tg:%s", chat_id)
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": int(chat_id),
        "text": body[:TELEGRAM_TEXT_LIMIT],
    }).encode("utf-8")
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
