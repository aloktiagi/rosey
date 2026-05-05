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
import io
import logging
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

import roster
import scheduler as reminder_scheduler
import transcribe  # voice → Whisper
from agent import handle_message
from paths import memories_dir

log = logging.getLogger("rosey.telegram")

# One asyncio.Lock per chat — prevents two concurrent messages in the same
# chat from racing into the agent loop and double-spending the ITPM budget.
# defaultdict gets us lazy creation; the dict grows by one entry per active
# chat, which is fine for a household-scale deployment.
_chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Path to the file we use to persist the last-processed Telegram update_id
# so a crash + restart doesn't redeliver and re-process the same message.
# Lives next to the memory directory rather than inside it (the agent's
# memory tool doesn't need to see this).
def _state_path() -> Path:
    return memories_dir().parent / ".telegram_state"


def _load_last_update_id() -> int:
    try:
        return int(_state_path().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return 0


def _save_last_update_id(uid: int) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(uid), encoding="utf-8")
    except OSError:
        log.exception("failed to persist last update_id")

# Trigger words that count as "addressing Rosey" in a group chat. Matched
# case-insensitively, must be the first word and followed by space/comma/colon
# (or be the whole message).
_NAME_PREFIXES = ("hey rosey", "rosey")


def _is_authorized(chat, user_id: int) -> bool:
    """Trust everyone if the roster has no Telegram entries (initial
    setup); otherwise:
      - in DMs, the chat_id (== user's id) must be in the roster;
      - in groups, either the *speaker's* user_id or the group's chat_id
        must be in the roster. This lets you choose between (a) listing
        each family member individually as `tg:<user_id>` (their DM id),
        or (b) listing the whole group as `tg:<negative_group_id>`.
    """
    tg_ids = roster.telegram_chat_ids()
    if not tg_ids:
        # Open mode for initial setup. (If the roster has only phone
        # members, this also evaluates True — that's intentional; the
        # host opts Telegram in by adding a `tg:` entry.)
        return True
    if chat.type == "private":
        return chat.id in tg_ids
    # group / supergroup / channel
    return user_id in tg_ids or chat.id in tg_ids


def _unauthorized_message(name: str, chat_id: int) -> str:
    return (
        f"Hi {name}! This Rosey is set up for a specific household.\n\n"
        f"If you should have access, ask whoever's running it to add this "
        f"line to /memories/household.md:\n\n"
        f"- {name} (tg:{chat_id})"
    )


def _bot_addressed(update, bot_username: str | None, bot_id: int | None) -> tuple[bool, str]:
    """Decide whether a group-chat message is directed at Rosey, and
    return the message text with the trigger stripped.

    Returns (should_respond, cleaned_text).

    DMs always respond and pass text through unchanged. In groups, we
    respond when ANY of these hold:
      1. The message is a reply to one of the bot's own messages.
      2. The message contains an @-mention of the bot's username.
      3. The message starts with "rosey" or "hey rosey" (case-insensitive),
         followed by space/comma/colon — or is exactly that prefix.

    Slash commands don't reach here (filtered upstream by ~filters.COMMAND).
    """
    chat = update.effective_chat
    msg = update.message
    text = (msg.text if msg else "") or ""
    text = text.strip()

    if chat.type == "private":
        return True, text

    # 1. Reply to one of the bot's messages.
    reply_to = getattr(msg, "reply_to_message", None)
    if (
        reply_to is not None
        and bot_id is not None
        and reply_to.from_user is not None
        and reply_to.from_user.id == bot_id
    ):
        return True, text

    # 2. @-mention. Telegram preserves the literal "@username" in the text
    # for `mention` entities, so a substring match is sufficient and avoids
    # walking entity offsets.
    if bot_username:
        handle = f"@{bot_username}"
        if re.search(re.escape(handle), text, flags=re.IGNORECASE):
            cleaned = re.sub(re.escape(handle), "", text, flags=re.IGNORECASE)
            cleaned = cleaned.strip(" ,:")
            return True, cleaned or text

    # 3. Name-prefix triggers.
    lower = text.lower()
    for prefix in _NAME_PREFIXES:
        if lower == prefix:
            return True, ""
        if lower.startswith(prefix) and len(text) > len(prefix) and text[len(prefix)] in " ,:":
            cleaned = text[len(prefix) :].lstrip(" ,:").strip()
            return True, cleaned

    return False, text


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

def _is_group(chat) -> bool:
    return chat.type in ("group", "supergroup")


def _voice_addressed_in_group(msg, bot_id: int | None) -> bool:
    """Voice messages in groups are only processed when they reply to one
    of the bot's own messages — there's no audio @-mention to detect, and
    transcribing-then-deciding would burn Whisper calls on every group
    voice note. In DMs, all voice is processed (caller checks `_is_group`).
    """
    reply_to = getattr(msg, "reply_to_message", None)
    return (
        reply_to is not None
        and bot_id is not None
        and reply_to.from_user is not None
        and reply_to.from_user.id == bot_id
    )


async def _on_start(update, context):
    chat = update.effective_chat
    user = update.effective_user
    name = user.first_name or "there"
    if _is_authorized(chat, user.id):
        await update.message.reply_text(
            f"Hi {name}! I'm Rosey — your household's shared memory. "
            "Try 'add bananas to the list' or 'remember the wifi password is goldfinch42'."
        )
    elif not _is_group(chat):
        # In DMs, give the standard onboarding message. In groups, just
        # stay silent — replying to /start from a stranger would be noise.
        await update.message.reply_text(_unauthorized_message(name, chat.id))


async def _run_agent(sender_id: str, body: str) -> str:
    # handle_message is synchronous and can block on Anthropic + memory I/O.
    # Run it off the event loop so the bot stays responsive.
    return await asyncio.to_thread(handle_message, sender_id, body)


# Loaded at import; updated as messages are processed. Backstop against
# Telegram redelivery on restart-after-crash. Save BEFORE processing so a
# crash mid-agent-loop doesn't redeliver and re-execute (e.g. add milk twice).
_last_update_id = _load_last_update_id()


def _seen_or_advance(uid: int) -> bool:
    """Return True if this update was already processed (skip), False if
    new (and persist as latest). Monotonic: we only advance forward.
    """
    global _last_update_id
    if uid <= _last_update_id:
        return True
    _last_update_id = uid
    _save_last_update_id(uid)
    return False


async def _on_text(update, context):
    chat = update.effective_chat
    user = update.effective_user
    name = user.first_name or ""
    text = (update.message.text or "").strip()
    if not text:
        return

    bot_username = getattr(context.bot, "username", None)
    bot_id = getattr(context.bot, "id", None)

    # Group: gate on explicit address. Strip the trigger so the agent
    # sees "add milk" instead of "@rosey_bot add milk".
    if _is_group(chat):
        addressed, text = _bot_addressed(update, bot_username, bot_id)
        if not addressed:
            return  # ignore background chatter silently
        if not text:
            # Pure mention with no content — nudge the speaker.
            await update.message.reply_text(
                "Yes? Add what you need — e.g. 'rosey add milk to the list'."
            )
            return

    if not _is_authorized(chat, user.id):
        if _is_group(chat):
            return  # silent in groups; onboarding only happens in DMs
        await update.message.reply_text(_unauthorized_message(name, chat.id))
        return

    # Idempotency: skip if Telegram redelivered an update we already processed.
    if _seen_or_advance(update.update_id):
        log.info("skip already-seen update_id=%d", update.update_id)
        return

    # sender_id always identifies the human speaker, not the chat. In DMs
    # chat.id == user.id; in groups they differ and we want the speaker.
    sender_id = f"tg:{user.id}"
    log.info("inbound from=%s chat=%s len=%d", sender_id, chat.id, len(text))

    # Per-chat lock: if a prior message in this chat is still being
    # processed, queue behind it instead of spawning a parallel agent
    # loop. Prevents two agent loops in the same chat from doubling up
    # on the API token-per-minute budget.
    async with _chat_locks[chat.id]:
        try:
            reply = await _run_agent(sender_id, text)
        except Exception:
            log.exception("agent failure for %s", sender_id)
            reply = "Something went wrong. Try again in a moment."

    if reply:
        await update.message.reply_text(reply)


async def _on_voice(update, context):
    """Telegram voice notes (.oga / Opus) → Whisper → agent."""
    chat = update.effective_chat
    user = update.effective_user
    bot_id = getattr(context.bot, "id", None)

    # Group: only process voice notes that reply to the bot. No audio
    # @-mention detection without transcribing first, and we don't want
    # to burn a Whisper call per group voice note.
    if _is_group(chat) and not _voice_addressed_in_group(update.message, bot_id):
        return

    if not _is_authorized(chat, user.id):
        if _is_group(chat):
            return
        name = user.first_name or ""
        await update.message.reply_text(_unauthorized_message(name, chat.id))
        return

    if _seen_or_advance(update.update_id):
        log.info("skip already-seen voice update_id=%d", update.update_id)
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
        log.exception("transcription failed for tg:%s", user.id)
        await update.message.reply_text(
            "I couldn't hear that — try again or send text."
        )
        return

    if not transcript.strip():
        await update.message.reply_text(
            "I couldn't make out any speech. Try again?"
        )
        return

    sender_id = f"tg:{user.id}"
    log.info("inbound voice from=%s chat=%s transcript_len=%d",
             sender_id, chat.id, len(transcript))
    async with _chat_locks[chat.id]:
        try:
            reply = await _run_agent(sender_id, transcript)
        except Exception:
            log.exception("agent failure for %s (voice)", sender_id)
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

    # Start the persistent reminder scheduler before we begin polling.
    # reconcile() loads any past-due jobs and (with misfire_grace_time)
    # they fire immediately; future-due jobs sit in the SQLite jobstore
    # until their DateTrigger time arrives.
    reminder_scheduler.start()
    reminder_scheduler.reconcile()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
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
