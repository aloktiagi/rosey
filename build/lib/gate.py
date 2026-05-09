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
