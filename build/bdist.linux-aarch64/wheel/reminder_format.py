"""Single source of truth for the reminders.md line format.

The agent (agent.py system prompt) tells Claude how to write entries;
the scheduler (scheduler.py) parses them. Both pull from this module so
a format change can't drift between them.
"""
from __future__ import annotations

import re

# What the agent is instructed to produce. Used verbatim in the system prompt.
FORMAT_DOC = "- [YYYY-MM-DD HH:MM] short message @Name1 @Name2 from:tg:<chat_id>"

# What the parser matches. Two capture groups:
#   1: the timestamp ("YYYY-MM-DD HH:MM" or with a "T" between date and time)
#   2: the message portion (incl. any @mentions, from: tag, id:, esc:, miss:,
#      and any (annotation) parens added by the scheduler/agent)
LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\]\s+(.+?)\s*$")

# @Name in the message portion. Names are matched case-insensitively against
# household.md entries.
MENTION_RE = re.compile(r"@(\w+)")

# `from:tg:<chat_id>` tag — origin of the reminder. Used as a recipient
# fallback when @-named mentions don't resolve in the roster (e.g. roster
# is empty or the agent wrote a name that isn't listed). chat_id may be
# negative (group chats) or positive (DMs); both are valid Telegram ids.
FROM_RE = re.compile(r"\bfrom:(tg:-?\d+)\b")

# `id:<hex>` tag — stable task identifier. Assigned by the reconciler if
# the agent didn't write one. Used as the suffix on APScheduler job IDs
# (fire:<id>, escalate:<id>, miss:<id>) and as the link target for
# reply-to-bot acks.
ID_RE = re.compile(r"\bid:([a-f0-9]{6,32})\b")

# `esc:Nm` / `esc:Nh` / `esc:Nd` — escalation cadence override. When omitted,
# reconciler uses the default (30 minutes). The agent is instructed to set
# tighter values (e.g. esc:5m) for time-critical tasks and looser ones
# (esc:1d) for slow-burn tasks.
ESC_RE = re.compile(r"\besc:(\d+)([mhd])\b")

# `miss:Nh` / `miss:Nd` — give-up cadence override. Default is 24h. Past
# this, the line moves to ## Missed.
MISS_RE = re.compile(r"\bmiss:(\d+)([hd])\b")

# Annotations the scheduler (or agent, on natural-language ack) appends in
# parens at the end of the line. Order shown is the natural lifecycle:
#   (fired at T chat:C msg:M)         — primary fire happened
#   (escalated to group at T)          — fanned out because no ack in time
#   (acked by Name at T)               — terminal: handled
#   (missed at T)                      — terminal: gave up
ACKED_RE = re.compile(r"\(acked\b")
ESCALATED_RE = re.compile(r"\(escalated\b")
FIRED_AT_RE = re.compile(r"\(fired at ")
# Capture the (chat:C msg:M) pair from a fired-annotation, used for
# reply-to-bot ack lookup.
FIRED_CHAT_MSG_RE = re.compile(r"chat:(tg:-?\d+)\s+msg:(\d+)")
