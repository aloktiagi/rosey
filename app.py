"""Flask app for the household VM.

Exposes:
  POST /telegram         — receives forwarded Telegram messages from the
                            router; runs the agent; replies via the
                            Telegram bot API.
  POST /admin/add-member — appends a member line to household.md (called
                            by the router after invite-code redemption).
  GET  /health           — Fly health check.
"""
from __future__ import annotations

import hmac
import logging
import os
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from flask import Flask, Response, abort, request

from agent import handle_message
from paths import memories_dir

load_dotenv()
log = logging.getLogger("rosey")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = Flask(__name__)


def _maybe_start_scheduler() -> None:
    """Start the Saturday 9am summary cron + 1-min reminders polling
    in-process. Opt-in via env var so tests and local dev don't spin up
    schedulers."""
    if os.environ.get("ROSEY_ENABLE_SCHEDULER") != "1":
        return
    from summary import run_once  # local import — defer Telegram pulls until needed
    import reminders

    tz = os.environ.get("SCHEDULER_TZ", "UTC")
    scheduler = BackgroundScheduler(timezone=tz)
    scheduler.add_job(run_once, CronTrigger(day_of_week="sat", hour=9, minute=0))
    scheduler.add_job(
        reminders.check_due,
        IntervalTrigger(minutes=1),
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    log.info("scheduler started — Saturday 09:00 %s + reminders every 1m", tz)


def _maybe_bootstrap_household_md() -> None:
    """If /data/memories/household.md doesn't exist and HOUSEHOLD_TOML is
    set, render and write it. Used by per-household VMs in the SaaS setup
    where members are passed via env var instead of a baked-in file.
    """
    toml_str = os.environ.get("HOUSEHOLD_TOML")
    if not toml_str:
        return

    target_dir = memories_dir()
    target = target_dir / "household.md"
    if target.exists():
        return

    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python <3.11
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        from household import render

        config = tomllib.loads(toml_str)
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(render(config), encoding="utf-8")
        log.info("bootstrapped household.md from HOUSEHOLD_TOML env var")
    except Exception:
        log.exception("HOUSEHOLD_TOML bootstrap failed")


_maybe_bootstrap_household_md()
_maybe_start_scheduler()


def _request_is_trusted_internal() -> bool:
    """True when the inbound request carries a matching X-Rosey-Internal-Token.

    The router signs every forward with this header so the household VM
    can skip per-request signature checks (the underlying network is
    Fly's private 6PN; this header is the second factor).
    """
    expected = os.environ.get("ROSEY_INTERNAL_TOKEN")
    if not expected:
        return False
    provided = request.headers.get("X-Rosey-Internal-Token", "")
    return bool(provided) and hmac.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Telegram inbound (forwarded by router)
# ---------------------------------------------------------------------------

def _send_telegram_message(chat_id: int, text: str) -> bool:
    """Reply to a Telegram chat using the bot token. Stateless POST."""
    import json as _json
    import urllib.error
    import urllib.request

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot reply to tg:%s", chat_id)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = _json.dumps({"chat_id": chat_id, "text": text[:4096]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        log.error(
            "telegram send failed to tg:%s status=%s body=%s",
            chat_id, e.code,
            e.read().decode("utf-8", errors="replace")[:300],
        )
        return False
    except Exception:
        log.exception("telegram send failed to tg:%s", chat_id)
        return False


def _process_telegram_and_reply(
    chat_id: int,
    text: str,
    image_b64: str | None = None,
    image_mime: str | None = None,
) -> None:
    sender_id = f"tg:{chat_id}"
    try:
        reply = handle_message(
            sender_id, text, image_b64=image_b64, image_mime=image_mime,
        )
    except Exception:
        log.exception("agent failure for %s", sender_id)
        reply = "Something went wrong. Try again in a moment."
    if not reply:
        return
    _send_telegram_message(chat_id, reply)


@app.post("/telegram")
def telegram_inbound() -> Response:
    """Receive a parsed Telegram message forwarded by the router.
    Expects JSON `{chat_id, text, name?, image_b64?, image_mime?}` with
    X-Rosey-Internal-Token.
    """
    if not _request_is_trusted_internal():
        log.warning("telegram: missing/bad internal token")
        abort(403)

    payload = request.get_json(silent=True) or {}
    chat_id = payload.get("chat_id")
    text = (payload.get("text") or "").strip()
    image_b64 = payload.get("image_b64")
    image_mime = payload.get("image_mime")
    if not isinstance(chat_id, int):
        return Response("", status=200)
    if not text and not image_b64:
        return Response("", status=200)

    log.info(
        "telegram inbound from=tg:%s len=%d photo=%s",
        chat_id, len(text), "yes" if image_b64 else "no",
    )
    threading.Thread(
        target=_process_telegram_and_reply,
        args=(chat_id, text, image_b64, image_mime),
        daemon=True,
    ).start()
    return Response("", status=200)


# ---------------------------------------------------------------------------
# Admin: add a member (called by router after invite-code redemption)
# ---------------------------------------------------------------------------

@app.post("/admin/add-member")
def admin_add_member() -> Response:
    """Append a new member line to /memories/household.md. Idempotent."""
    if not _request_is_trusted_internal():
        log.warning("admin/add-member: missing/bad internal token")
        abort(403)

    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    identifier = (payload.get("identifier") or "").strip()
    if not name or not identifier:
        return Response("missing name or identifier", status=400)

    target_dir = memories_dir()
    target = target_dir / "household.md"
    target_dir.mkdir(parents=True, exist_ok=True)

    if target.exists():
        existing = target.read_text(encoding="utf-8")
    else:
        existing = "# Household\n\nMembers:\n"

    if f"({identifier})" in existing:
        log.info("admin/add-member: %s already present, skipping", identifier)
        return Response("", status=200)

    new_line = f"- {name} ({identifier})\n"
    if "Members:\n" in existing:
        head, _, rest = existing.partition("Members:\n")
        rest_lines = rest.splitlines(keepends=True)
        i = 0
        while i < len(rest_lines) and rest_lines[i].startswith("- "):
            i += 1
        new_content = head + "Members:\n" + "".join(rest_lines[:i]) + new_line + "".join(rest_lines[i:])
    else:
        new_content = existing.rstrip() + "\n" + new_line

    target.write_text(new_content, encoding="utf-8")
    log.info("admin/add-member: added %s (%s)", name, identifier)
    return Response("", status=200)


@app.get("/health")
def health() -> Response:
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
