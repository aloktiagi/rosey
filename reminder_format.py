"""Single source of truth for the reminders.md line format.

The agent (agent.py system prompt) tells Claude how to write entries;
the cron (reminders.py) parses them. Both pull from this module so a
format change can't drift between them.
"""
from __future__ import annotations

import re

# What the agent is instructed to produce. Used verbatim in the system prompt.
FORMAT_DOC = "- [YYYY-MM-DD HH:MM] short message @Name1 @Name2"

# What the parser matches. Two capture groups:
#   1: the timestamp ("YYYY-MM-DD HH:MM" or with a "T" between date and time)
#   2: the message portion (incl. any @mentions)
LINE_RE = re.compile(r"^- \[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\]\s+(.+?)\s*$")

# @Name in the message portion. Names are matched case-insensitively against
# household.md entries.
MENTION_RE = re.compile(r"@(\w+)")
