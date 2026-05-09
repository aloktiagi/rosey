"""Single-tenant Telegram adapter for Rosey.

Listens on a Telegram bot token, identifies senders by chat_id, and runs
the existing agent (memory tool, web search, reminders, voice notes) on
their messages. One bot = one household.

Polling mode (default): zero infrastructure — laptop, Pi, anywhere with
internet. Set TELEGRAM_BOT_TOKEN and run:

    python -m telegram_bot

Webhook mode (for Fly / public hosts): set TELEGRAM_WEBHOOK_URL to a
public HTTPS endpoint and the bot registers it instead of polling.

Household roster: edit /memories/household.md to list each family member
on a line like `- Alex (tg:12345678)`. Anyone whose chat_id isn't in the
roster gets a polite "ask the host to add you" reply with their chat_id —
the host then pastes it into household.md and reloads the bot.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import sys

from dotenv import load_dotenv

import roster
import transcribe  # voice → Whisper
from agent import handle_message

log = logging.getLogger("rosey.telegram")


def _is_authorized(chat_id: int) -> bool:
    """Trust everyone if the roster has no Telegram entries (initial
    setup); otherwise the chat_id must appear as `tg:NNN` in
    household.md."""
    tg_ids = roster.telegram_chat_ids()
    if not tg_ids:
        # Roster has no Telegram members yet — open mode, trust everyone.
        # (If the roster has only phone members, this also evaluates True;
        # that's intentional — the host is opting Telegram in by adding a
        # tg: entry.)
        return True
    return chat_id in tg_ids


def _unauthorized_message(name: str, chat_id: int) -> str:
    return (
        f"Hi {name}! This Rosey is set up for a specific household.\n\n"
        f"If you should have access, ask whoever's running it to add this "
        f"line to /memories/household.md:\n\n"
        f"- {name} (tg:{chat_id})"
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def _on_start(update, context):
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or "there"
    if _is_authorized(chat_id):
        await update.message.reply_text(
            f"Hi {name}! I'm Rosey — your household's shared memory. "
            "Try 'add bananas to the list' or 'remember the wifi password is goldfinch42'."
        )
    else:
        await update.message.reply_text(_unauthorized_message(name, chat_id))


async def _run_agent(
    sender_id: str,
    body: str,
    image_b64: str | None = None,
    image_mime: str | None = None,
) -> str:
    # handle_message is synchronous and can block on Anthropic + memory I/O.
    # Run it off the event loop so the bot stays responsive.
    return await asyncio.to_thread(
        handle_message, sender_id, body,
        image_b64=image_b64, image_mime=image_mime,
    )


async def _on_text(update, context):
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or ""
    text = (update.message.text or "").strip()
    if not text:
        return

    if not _is_authorized(chat_id):
        await update.message.reply_text(_unauthorized_message(name, chat_id))
        return

    log.info("inbound from=tg:%s len=%d", chat_id, len(text))
    try:
        reply = await _run_agent(f"tg:{chat_id}", text)
    except Exception:
        log.exception("agent failure for tg:%s", chat_id)
        reply = "Something went wrong. Try again in a moment."

    if reply:
        await update.message.reply_text(reply)


async def _on_photo(update, context):
    """Telegram photos → multimodal agent turn."""
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or ""

    if not _is_authorized(chat_id):
        await update.message.reply_text(_unauthorized_message(name, chat_id))
        return

    photos = update.message.photo or []
    if not photos:
        return
    largest = photos[-1]  # progressively-bigger thumbnails; last = full size
    caption = (update.message.caption or "").strip()

    try:
        tg_file = await context.bot.get_file(largest.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        image_bytes = buf.getvalue()
    except Exception:
        log.exception("photo download failed for tg:%s", chat_id)
        await update.message.reply_text("I couldn't grab that photo — try again?")
        return

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    log.info(
        "inbound photo from=tg:%s bytes=%d caption_len=%d",
        chat_id, len(image_bytes), len(caption),
    )
    try:
        reply = await _run_agent(
            f"tg:{chat_id}", caption,
            image_b64=image_b64, image_mime="image/jpeg",
        )
    except Exception:
        log.exception("agent failure for tg:%s (photo)", chat_id)
        reply = "Something went wrong. Try again in a moment."

    if reply:
        await update.message.reply_text(reply)


async def _on_voice(update, context):
    """Telegram voice notes (.oga / Opus) → Whisper → agent."""
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name or ""

    if not _is_authorized(chat_id):
        await update.message.reply_text(_unauthorized_message(name, chat_id))
        return

    voice = update.message.voice or update.message.audio
    if voice is None:
        return

    try:
        tg_file = await context.bot.get_file(voice.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(out=buf)
        audio_bytes = buf.getvalue()
        # Telegram voice is OGG Opus
        transcript = await asyncio.to_thread(
            transcribe.transcribe_audio, audio_bytes, "audio/ogg"
        )
    except Exception:
        log.exception("transcription failed for tg:%s", chat_id)
        await update.message.reply_text(
            "I couldn't hear that — try again or send text."
        )
        return

    if not transcript.strip():
        await update.message.reply_text(
            "I couldn't make out any speech. Try again?"
        )
        return

    log.info("inbound voice from=tg:%s transcript_len=%d", chat_id, len(transcript))
    try:
        reply = await _run_agent(f"tg:{chat_id}", transcript)
    except Exception:
        log.exception("agent failure for tg:%s (voice)", chat_id)
        reply = "Something went wrong. Try again in a moment."

    if reply:
        await update.message.reply_text(reply)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        from telegram import Update  # noqa: F401
        from telegram.ext import (
            Application,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError:
        print(
            "python-telegram-bot is not installed. Install with:\n"
            "    pip install 'rosey[telegram]'\n"
            "or:\n"
            "    pip install 'python-telegram-bot>=21'",
            file=sys.stderr,
        )
        return 1

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather.", file=sys.stderr)
        return 1

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.PHOTO, _on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _on_voice))

    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL")
    if webhook_url:
        port = int(os.environ.get("PORT", "8080"))
        log.info("starting webhook on :%d → %s", port, webhook_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="telegram",
            webhook_url=webhook_url.rstrip("/") + "/telegram",
        )
    else:
        log.info("starting Telegram polling — Ctrl-C to quit")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
