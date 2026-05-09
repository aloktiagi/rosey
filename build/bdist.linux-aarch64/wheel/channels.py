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


def send(identifier: str, body: str, parse_mode: str | None = None) -> bool:
    """Dispatch outbound message by identifier prefix.

    Returns True on success, False if creds are missing or the API
    call failed. Errors are logged, not raised — fan-out callers should
    keep going for the other recipients.
    """
    return send_returning_msg_id(identifier, body, parse_mode=parse_mode) is not None


def send_returning_msg_id(
    identifier: str, body: str, parse_mode: str | None = None,
) -> int | None:
    """Like send() but returns the platform message_id on success.

    `parse_mode` is forwarded to Telegram unchanged — values like "HTML"
    or "MarkdownV2" enable rich-text rendering (e.g. mention links via
    `<a href="tg://user?id=NNN">Name</a>`). Default None = plain text.
    """
    if identifier.startswith("tg:"):
        return send_telegram(identifier[len("tg:"):], body, parse_mode=parse_mode)
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
