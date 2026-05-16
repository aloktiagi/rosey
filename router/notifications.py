"""Thin wrapper around the Telegram Bot API for outbound messages.

Centralizes the sendMessage / sendChatAction calls so app.py and
provisioning.py share one implementation. All functions return True on
2xx success, False otherwise; failures are logged but never raise — the
caller decides whether to retry.

Bot token comes from the ``TELEGRAM_BOT_TOKEN`` environment variable;
this module assumes it's set in production (router and host VM both
have it as a Fly secret).
"""
from __future__ import annotations

import json as _json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_MAX_LEN = 4096


def _post(path: str, payload: dict, timeout: int = 10) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot %s", path)
        return False
    url = f"https://api.telegram.org/bot{token}/{path}"
    data = _json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        log.error("%s failed status=%s body=%s", path, e.code, body)
        return False
    except Exception:
        log.exception("%s failed", path)
        return False


def send_text(chat_id: int, text: str, parse_mode: Optional[str] = None) -> bool:
    """Send a plain text message. ``parse_mode`` may be 'Markdown' or 'HTML'."""
    payload: dict = {"chat_id": chat_id, "text": text[:_MAX_LEN]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _post("sendMessage", payload)


def send_with_url_button(
    chat_id: int,
    text: str,
    button_label: str,
    button_url: str,
    parse_mode: Optional[str] = None,
) -> bool:
    """Send a message with a single inline URL button.

    URL buttons are the simplest interactive control: tapping opens the
    URL in the user's default handler. For Telegram deep links like
    ``https://t.me/<bot>?startgroup=<payload>`` this opens the
    add-to-group picker pre-loaded with the bot.
    """
    payload: dict = {
        "chat_id": chat_id,
        "text": text[:_MAX_LEN],
        "reply_markup": {
            "inline_keyboard": [
                [{"text": button_label, "url": button_url}]
            ]
        },
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    return _post("sendMessage", payload)


def leave_chat(chat_id: int) -> bool:
    """Remove the bot from a group. Used when an unauthorized user adds
    the bot to a random group (no household linkage)."""
    return _post("leaveChat", {"chat_id": chat_id})
