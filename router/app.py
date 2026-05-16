"""Router app: Telegram webhook entry point.

For each inbound Telegram message, look up the sender in the tenant DB and either:
  - forward to that household's per-VM Fly app (via Fly 6PN); or
  - hand to the Telegram-native onboarding FSM for unknown senders.

Identifier scheme: `tg:NNN` where NNN is the Telegram chat_id.
"""

from __future__ import annotations

import hmac
import json as json_mod
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, request

from . import db, provisioning, telegram_onboarding

FEEDBACK_PREFIXES = ("/feedback", "/contact", "/support")

load_dotenv()
log = logging.getLogger("rosey.router")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = Flask(__name__)
engine = db.get_engine()
db.init_db(engine)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _is_feedback(body: str) -> bool:
    lower = body.lower().lstrip()
    return any(lower == p or lower.startswith(p + " ") for p in FEEDBACK_PREFIXES)


def _strip_feedback_prefix(body: str) -> str:
    lower = body.lower().lstrip()
    for p in FEEDBACK_PREFIXES:
        if lower.startswith(p):
            return body.lstrip()[len(p) :].strip()
    return body.strip()


def _send_feedback(member: dict, sender_id: str, message: str) -> None:
    """Forward a /feedback message from a household member to the operator
    via Telegram. Operator chat_id configured via env var.
    """
    operator_id = os.environ.get("ROSEY_OPERATOR_TELEGRAM_ID")
    if not operator_id:
        log.warning("feedback dropped — ROSEY_OPERATOR_TELEGRAM_ID not set")
        return

    text = (
        f"📝 Rosey feedback from {member['name']} ({sender_id})\n"
        f"household {member['household_id'][:8]}\n\n"
        f"{message}"
    )
    try:
        _send_telegram_message(int(operator_id), text[:4096])
        log.info(
            "feedback forwarded to operator from=%s len=%d", sender_id, len(message)
        )
    except Exception:
        log.exception("feedback forward failed from=%s", sender_id)


def _send_telegram_message(chat_id: int, text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot send tg:%s", chat_id)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json_mod.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        log.error(
            "telegram send failed to tg:%s status=%s body=%s",
            chat_id,
            e.code,
            e.read().decode("utf-8", errors="replace")[:300],
        )
        return False
    except Exception:
        log.exception("telegram send failed to tg:%s", chat_id)
        return False


def _post_to_household(
    fly_app_name: str,
    path: str,
    payload: dict,
    extra_headers: Optional[dict] = None,
) -> bool:
    """POST JSON to a household VM endpoint over 6PN. Returns True on 2xx."""
    base = os.environ.get("ROSEY_HOUSEHOLD_BASE_URL")
    if base:
        url = f"{base.rstrip('/')}{path}"
    else:
        url = f"http://{fly_app_name}.internal:8080{path}"
    token = os.environ.get("ROSEY_INTERNAL_TOKEN")
    headers = {
        "X-Rosey-Internal-Token": token or "",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        log.info("posted %s to %s status=%d", path, fly_app_name, r.status_code)
        return 200 <= r.status_code < 300
    except Exception:
        log.exception("post %s to %s failed", path, fly_app_name)
        return False


def _forward_to_household_telegram(
    fly_app_name: str, update: dict, telegram_secret: str
) -> None:
    """Forward a raw Telegram update to the household VM's /telegram endpoint.

    The household VM runs python-telegram-bot which expects the original
    webhook shape (Update.de_json). We pass the secret token through so
    server.py's signature check passes; the router has already validated
    it on the inbound side.
    """
    _post_to_household(
        fly_app_name,
        "/telegram",
        update,
        extra_headers={"X-Telegram-Bot-Api-Secret-Token": telegram_secret},
    )


# ---------------------------------------------------------------------------
# Telegram inbound webhook
# ---------------------------------------------------------------------------


def _validate_telegram_secret() -> None:
    """Telegram lets us register a webhook with a secret_token; it sends it
    back as X-Telegram-Bot-Api-Secret-Token. Use it as a cheap auth check."""
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        log.warning("TELEGRAM_WEBHOOK_SECRET not set — skipping telegram auth")
        return
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("rejected telegram webhook with bad secret_token")
        abort(403)


def _telegram_extract(update: dict) -> tuple:
    """Parse the bits of a Telegram update we care about. Returns
    (chat_id, text, first_name, photo_file_id) — photo_file_id is the
    largest variant's file_id when the message has a photo, else None.
    Returns (None, None, None, None) for irrelevant update types.
    """
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None, None, None, None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()
    sender = msg.get("from") or {}
    first_name = sender.get("first_name") or ""
    # Telegram photos arrive as an array of size variants; the last one is
    # the largest. We forward only the largest to the household VM.
    photo = msg.get("photo") or []
    photo_file_id = photo[-1]["file_id"] if photo else None
    return chat_id, text, first_name, photo_file_id


@app.post("/telegram")
def telegram_webhook() -> Response:
    _validate_telegram_secret()

    update = request.get_json(silent=True) or {}
    chat_id, text, first_name, photo_file_id = _telegram_extract(update)
    if chat_id is None:
        return Response("", status=200)
    if not text and not photo_file_id:
        # Voice notes, stickers, etc. Ack so Telegram doesn't retry.
        return Response("", status=200)

    sender_id = f"tg:{chat_id}"
    log.info(
        "telegram inbound from=%s len=%d photo=%s",
        sender_id,
        len(text),
        "yes" if photo_file_id else "no",
    )

    member = db.lookup_household(engine, sender_id)
    if member:
        # /invite <name> — admin command to generate an invite code
        if text.lower().lstrip().startswith("/invite"):
            invitee = text.lstrip()[len("/invite") :].strip()
            if not invitee:
                _send_telegram_message(
                    chat_id,
                    "Send: /invite <their name> — I'll give you a code to share with them.",
                )
                return Response("", status=200)
            code = telegram_onboarding.generate_invite_code(engine, member, invitee)
            bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "the Rosey bot")
            _send_telegram_message(
                chat_id,
                f"Share this with {invitee}:\n\n"
                f"Open @{bot_username} and send: {code}\n\n"
                "(Expires in 7 days.)",
            )
            return Response("", status=200)

        # /feedback intercept (text-only)
        if text and _is_feedback(text):
            note = _strip_feedback_prefix(text)
            if not note:
                _send_telegram_message(
                    chat_id,
                    "Send /feedback followed by your message — I'll pass it on.",
                )
                return Response("", status=200)
            threading.Thread(
                target=_send_feedback,
                args=(member, sender_id, note),
                daemon=True,
            ).start()
            _send_telegram_message(chat_id, "Got it — passed your message along. 🙏")
            return Response("", status=200)

        # Forward the RAW Telegram update to the household VM. server.py
        # runs python-telegram-bot which expects the original webhook shape
        # (Update.de_json + process_update). Photos are downloaded by the
        # household VM's PTB handlers, not pre-downloaded here.
        inbound_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        threading.Thread(
            target=_forward_to_household_telegram,
            args=(member["fly_app_name"], update, inbound_secret),
            daemon=True,
        ).start()
        return Response("", status=200)

    # Unknown sender → Telegram-native onboarding FSM.
    if text.strip().lower() == "/start":
        text = ""
    reply = telegram_onboarding.handle(engine, chat_id, first_name, text)

    # If the reply implies a member was just added (invite redemption),
    # propagate to the household VM's household.md.
    if reply.text.startswith("🎉"):
        added = db.lookup_household(engine, sender_id)
        if added:
            threading.Thread(
                target=_post_to_household,
                args=(
                    added["fly_app_name"],
                    "/admin/add-member",
                    {"name": added["name"], "identifier": sender_id},
                ),
                daemon=True,
            ).start()

    if reply.trigger_provisioning:
        provisioning.kick_off(engine, sender_id)

    _send_telegram_message(chat_id, reply.text)
    return Response("", status=200)


@app.get("/health")
def health() -> Response:
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8081)))
