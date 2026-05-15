"""Fuzzy trigger gate for group chat.

In a family group chat, most messages aren't directed at the bot — they're
people talking to each other. The original bot ignores everything that
doesn't match an explicit trigger (@-mention, reply-to-bot, "rosey ..."
prefix). That's safe but it means useful requests like "we need milk" or
"who was the plumber's number again" go unanswered unless the speaker
remembers to address Rosey by name.

This module adds a cheap classifier — one Haiku call per un-addressed
group message — that decides whether the message is something a
household assistant should handle. The full Sonnet agent only runs when
the classifier says YES.

Cost: ~300 cached input tokens + ~3 output tokens per call. At Haiku 4.5
pricing that's a fraction of a cent. A chatty group at 100 messages/day
is a few cents a month.

Latency: typically 200–500ms. We bound it explicitly so a slow classifier
doesn't hang the bot — on timeout we fail closed and drop the message
(user can resend with @-mention if Rosey was actually wanted).

Bias: false positives (Rosey barging into private chat) are worse than
false negatives (user has to @-mention to get a response). The prompt and
the fail-closed default both reflect that bias.
"""
from __future__ import annotations

import logging
import os
import time

from anthropic import Anthropic

log = logging.getLogger("rosey.gate")

# Channel-agnostic name-prefix triggers. A group-chat message that
# starts with one of these (case-insensitive, followed by space, comma,
# or colon, or being the entire message) counts as explicitly addressed
# to Rosey, regardless of channel. Channel-specific signals like
# reply-to-bot or @-username mentions remain in the per-channel handler
# because they're protocol-level not text-level.
_NAME_PREFIXES = ("hey rosey", "rosey")

# Haiku 4.5 — fast and cheap. Sonnet for this would be wasteful; we
# only need a binary decision, no tools, no reasoning chain.
GATE_MODEL = "claude-haiku-4-5-20251001"
GATE_MAX_TOKENS = 4
GATE_TIMEOUT_S = 5.0

GATE_SYSTEM_PROMPT = """You are a router for a household assistant bot called Rosey. \
Rosey is the family's chief-of-staff: it helps with shared logistics like grocery lists, \
reminders, family knowledge (wifi passwords, vendor contacts, kid info), task assignments, \
research lookups, and tracking household to-dos.

You will see one message from a family group chat. Decide whether Rosey should respond. \
Output exactly one word: YES or NO. No explanation, no punctuation.

Answer YES when the message is:
- A question Rosey could plausibly answer (info lookup, "what's our wifi", "when is the pediatrician", "do we have milk")
- A request to add to a list, set a reminder, remember something, or look something up
- A report of finishing a household task ("got the milk", "called the plumber", "paid comcast")
- An instruction or assignment between family members ("Sam can you grab eggs tomorrow", "remind me to call mom")
- Information that should be remembered (account info, vendor numbers, kid sizes, schedules)
- A request for help with a household decision (vendor recommendation, recipe, planning)

Answer NO when the message is:
- Casual chat or banter between family members
- Emotional or relational conversation
- Reactions or short acknowledgments ("ok", "lol", "got it", "thanks", "👍", "k")
- Jokes, memes, or non-substantive social
- Clearly directed at a specific person, not addressed to the room/bot
- An explicit "don't bother Rosey" / "ignore that" / "nevermind"
- Photos, links, or media without surrounding instruction

When in doubt, answer NO. Rosey staying quiet is much better than Rosey barging in.

Output: YES or NO only."""


_client: Anthropic | None = None


def _get_client() -> Anthropic:
    """Lazy-init the Anthropic client. Same key as the main agent uses."""
    global _client
    if _client is None:
        _client = Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            timeout=GATE_TIMEOUT_S,
            max_retries=0,  # don't retry; on slow/failing call we'd rather drop the msg
        )
    return _client


def explicit_name_trigger(text: str) -> tuple[bool, str]:
    """Check if `text` is explicitly addressed to Rosey via a name
    prefix ("rosey ..." or "hey rosey ..." or "@rosey ..."), and
    return the message with the trigger stripped.

    Returns (matched, cleaned_text). Matching is case-insensitive and
    requires the prefix to be followed by space/comma/colon — so
    "rosey" matches "rosey, what's the wifi" but not "rosemary".

    A bare "rosey" or "@rosey" with nothing after returns (True, "")
    so callers can prompt the user to add a body.

    Strips a leading "@" before matching so WhatsApp/Telegram's
    formal @-mentions (rendered as literal "@Rosey ..." in the text
    payload by the time we see it) hit the trigger instead of
    falling through to the fuzzy classifier.
    """
    if not text:
        return False, ""
    # Drop a single leading @ if present — same prefix logic applies whether
    # the user typed "rosey, ..." or "@Rosey ...". Other leading chars
    # (whitespace, etc.) are preserved as-is to keep semantics tight.
    candidate = text[1:].lstrip() if text.startswith("@") else text
    lower = candidate.lower()
    for prefix in _NAME_PREFIXES:
        if lower == prefix:
            return True, ""
        if lower.startswith(prefix) and len(candidate) > len(prefix) and candidate[len(prefix)] in " ,:":
            cleaned = candidate[len(prefix):].lstrip(" ,:").strip()
            return True, cleaned
    return False, text


def classify_group_message(text: str) -> tuple[bool, str]:
    """Channel-agnostic group-chat gate: decide whether Rosey should
    respond and return the cleaned text (with any name prefix stripped).

    Combines two layers, fail-closed by default:
      1. Explicit name-prefix trigger ("rosey ..." or "hey rosey ...").
         If matched, returns (True, cleaned_text) immediately — no
         classifier call needed.
      2. Fuzzy classifier (`should_respond_in_group`) if enabled. Used
         to catch household-shaped requests that don't explicitly
         address Rosey (e.g. "we need milk", "who's the plumber").

    Protocol-specific signals — reply-to-bot, @-username mentions —
    are NOT handled here. Callers (telegram_bot, whatsapp_handler)
    should check those FIRST and only fall through to this helper for
    the text-level decision.

    Returns (should_respond, cleaned_text). When should_respond is
    False, cleaned_text is the unchanged original.
    """
    text = (text or "").strip()
    if not text:
        return False, ""

    matched, cleaned = explicit_name_trigger(text)
    if matched:
        return True, cleaned

    if not fuzzy_enabled():
        return False, text  # strict mode — only explicit triggers count

    return should_respond_in_group(text), text


def fuzzy_enabled() -> bool:
    """Feature flag. Default on; set ROSEY_FUZZY_TRIGGER=off (or 0/false/no)
    to disable and revert to strict explicit-trigger-only behavior.
    """
    val = os.environ.get("ROSEY_FUZZY_TRIGGER", "on").strip().lower()
    return val not in ("off", "0", "false", "no", "")


def should_respond_in_group(text: str) -> bool:
    """Classify a group-chat message. True = Rosey should respond, False = stay silent.

    Fail-closed: on any error (timeout, API failure, malformed response) we
    return False so an outage of the classifier reverts to strict mode
    rather than spraying replies everywhere.
    """
    if not text or not text.strip():
        return False

    t_start = time.monotonic()
    decision = False
    raw = ""
    error = None
    try:
        client = _get_client()
        # Cache the system prompt — same content every call, so iteration N>=2
        # reads from the cache (~10% input cost) and the latency drop is
        # noticeable too. The user message is the only varying part.
        response = client.messages.create(
            model=GATE_MODEL,
            max_tokens=GATE_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": GATE_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": f"Message:\n{text}\n\nDecision:"}],
        )
        raw = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        ).strip().upper()
        # Tolerate a stray period or quote — but only the first letter counts.
        decision = raw.startswith("Y")
    except Exception as e:
        error = str(e)[:120]
        decision = False  # fail closed

    dt_ms = int((time.monotonic() - t_start) * 1000)
    log.info(
        "gate decision=%s raw=%r len=%d wall_ms=%d%s",
        "YES" if decision else "NO",
        raw,
        len(text),
        dt_ms,
        f" error={error}" if error else "",
    )
    return decision
