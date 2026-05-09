"""Alexa skill webhook handler.

Receives Alexa request envelopes (LaunchRequest, IntentRequest,
SessionEndedRequest) from the Alexa Skills Kit and routes them through
Rosey's existing agent loop. The skill's interaction model defines a
single catch-all intent `RoseyCatchAllIntent` with an `AMAZON.SearchQuery`
slot named `query`. Whatever the user said after "Alexa, ask rosey to …"
lands in the slot and gets passed to `agent.handle_message()` exactly as
if it were a Telegram message.

This means the same trigger gating, fuzzy gate, agent loop, memory tool,
scheduler, and reminder lifecycle handle Alexa requests with zero code
duplication. Alexa is just a different mouth, same brain.

For v1: skips Alexa request signature verification (development-mode
skills can opt out, and ours is dev-mode for now). When/if we ever
distribute the skill publicly, we'd add SigV1 verification per:
https://developer.amazon.com/en-US/docs/alexa/custom-skills/host-a-custom-skill-as-a-web-service.html#manually-verify-request-sent-by-alexa
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

from agent import handle_message

log = logging.getLogger("rosey.alexa")

# Voice replies have stricter UX constraints than text — long monologues
# are painful to listen to. Telegram replies can be 200+ chars; for voice
# we cap to keep it human-listenable.
VOICE_REPLY_MAX_CHARS = 300

# Strip common markdown characters that Alexa's PlainText output speaks
# literally — '*milk*' becomes "star milk star". Conservative set so we
# don't accidentally munge real punctuation.
_MARKDOWN_CHARS_RE = re.compile(r"[*_`#~]")


def _clean_for_voice(text: str) -> str:
    """Sanitize agent reply for spoken output: strip markdown, collapse
    whitespace, truncate to a voice-friendly length.
    """
    s = _MARKDOWN_CHARS_RE.sub("", text)
    s = " ".join(s.split())
    if len(s) > VOICE_REPLY_MAX_CHARS:
        s = s[: VOICE_REPLY_MAX_CHARS - 1].rstrip() + "…"
    return s


def _speak(text: str, end_session: bool = True) -> dict:
    """Build an Alexa response envelope with a PlainText speech reply.
    end_session=False keeps the microphone open for follow-up — useful
    for LaunchRequest, not what we want on most intent replies (the user
    said "ask rosey to add milk", they don't want Rosey to keep listening).
    """
    return {
        "version": "1.0",
        "response": {
            "outputSpeech": {"type": "PlainText", "text": text},
            "shouldEndSession": end_session,
        },
    }


def _empty_response() -> dict:
    """For SessionEndedRequest — Alexa wants a 200, no speech."""
    return {"version": "1.0", "response": {"shouldEndSession": True}}


async def handle(envelope: dict) -> dict:
    """Top-level entry point. Dispatches by request type. Returns the
    Alexa response envelope as a dict (caller serializes to JSON).

    Three request types Alexa actually sends us:
      - LaunchRequest: user said "Alexa, open rosey" with no follow-up
      - IntentRequest: matched one of our defined intents (catch-all)
      - SessionEndedRequest: notification the conversation closed
    """
    request = envelope.get("request", {}) or {}
    request_type = request.get("type")

    if request_type == "LaunchRequest":
        # We didn't design for multi-turn conversation — most users will
        # always include their request in the same utterance ("ask rosey
        # to X"). For the bare "open rosey" case, prompt and end.
        return _speak("Hi. What can I do for you?", end_session=False)
    if request_type == "SessionEndedRequest":
        return _empty_response()
    if request_type == "IntentRequest":
        return await _handle_intent(envelope)

    log.info("alexa: unhandled request type %r", request_type)
    return _speak("Sorry, I'm not sure what to do with that.")


async def _handle_intent(envelope: dict) -> dict:
    intent = envelope["request"].get("intent", {}) or {}
    intent_name = intent.get("name", "")
    user_id = (
        envelope.get("session", {}).get("user", {}).get("userId")
        or envelope.get("context", {}).get("System", {}).get("user", {}).get("userId")
        or "unknown"
    )

    log.info("alexa: intent=%s user=%s", intent_name, user_id[:24] + "…" if len(user_id) > 24 else user_id)

    if intent_name == "AMAZON.HelpIntent":
        return _speak(
            "Ask me to add things to a list, set a reminder, or remember "
            "household info. For example: add milk to the shopping list, "
            "or remind me at three to call the pediatrician."
        )
    if intent_name in (
        "AMAZON.StopIntent",
        "AMAZON.CancelIntent",
        "AMAZON.NavigateHomeIntent",
    ):
        return _speak("Okay.")
    if intent_name == "RoseyCatchAllIntent":
        slot = intent.get("slots", {}).get("query", {}) or {}
        query = (slot.get("value") or "").strip()
        if not query:
            return _speak("I didn't catch that. Try again?")
        return await _dispatch_to_agent(query, user_id)

    log.info("alexa: unhandled intent %r", intent_name)
    return _speak("I'm not sure what to do with that.")


async def _dispatch_to_agent(query: str, alexa_user_id: str) -> dict:
    """Run the user's query through the existing agent loop and format
    the reply for voice.

    `sender_id` uses the `alexa:` prefix so the roster + scheduler can
    distinguish Alexa-originated messages from Telegram. For Alexa-set
    reminders, we use ALEXA_DEFAULT_ORIGIN_CHAT (typically the family
    Telegram group) as the from: tag fallback — Alexa itself can't
    receive push messages, so reminders need to fire elsewhere. If the
    env var is unset, the reminder will land in ## Failed_Delivery
    next reconcile, which is the desired loud-fail behavior.
    """
    sender_id = f"alexa:{alexa_user_id}"
    origin_chat = os.environ.get("ALEXA_DEFAULT_ORIGIN_CHAT", sender_id)

    try:
        reply = await asyncio.to_thread(
            handle_message, sender_id, query, origin_chat=origin_chat,
        )
    except Exception:
        log.exception("alexa: agent failure for %s", sender_id)
        return _speak("Something went wrong. Try again in a moment.")

    voice_reply = _clean_for_voice(reply or "Done.")
    log.info(
        "alexa: query_len=%d agent_reply_len=%d voice_len=%d",
        len(query), len(reply or ""), len(voice_reply),
    )
    return _speak(voice_reply)
