"""Parse the answer to "Who's in the family?" — one member per line.

Accepts free-form input like:

    Sarah @sarah_t
    Mom @lakshmi_tandon
    Dad
    - Ravi  @ravi_kid_07

Returns a list of ``Member`` dataclasses. Telegram usernames are
lowercased and stored without the leading "@"; members without a
username can still join via invite code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Telegram username rules (BotFather):
#   - 5–32 chars
#   - starts with a letter
#   - letters, digits, underscores
#   - case-insensitive on lookup but Telegram preserves the display case
TG_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})$")

# Strip leading bullet markers some users may include in a list.
_BULLET_RE = re.compile(r"^[\s\-\*•·]+")

# Phrases that mean "no one but me" — accept any of these as a skip.
_SKIP_TOKENS = {
    "skip",
    "none",
    "no",
    "n/a",
    "just me",
    "just me for now",
    "nobody",
    "no one",
}

NAME_MIN = 1
NAME_MAX = 80


@dataclass(frozen=True)
class Member:
    """One parsed member entry. ``tg_username`` is lowercased without "@"."""

    name: str
    tg_username: Optional[str] = None


def is_skip(text: str) -> bool:
    """True if the user's answer effectively means "no members to list"."""
    return (text or "").strip().lower() in _SKIP_TOKENS


def parse_member_line(line: str) -> Optional[Member]:
    """Parse a single line. Returns None for empty / unparseable lines."""
    line = _BULLET_RE.sub("", line or "").strip()
    if not line:
        return None

    tokens = line.split()
    username: Optional[str] = None
    name_tokens: list[str] = []

    for tok in tokens:
        if tok.startswith("@"):
            m = TG_USERNAME_RE.match(tok)
            if m:
                # Explicit @username — strongest signal
                username = m.group(1).lower()
                continue
            # Looks like an @-handle but doesn't validate. Fall through
            # to treating it as a name fragment (rare; user error).
        name_tokens.append(tok)

    name = " ".join(name_tokens).strip()
    if not name and username:
        # Line was just "@sarah_t" — use the username as the display name
        name = username
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        return None

    return Member(name=name, tg_username=username)


def parse_members(text: str) -> list[Member]:
    """Parse the full multi-line answer to "Who's in the family?".

    Returns [] if the user opted out via a skip token. Otherwise returns
    the parsed members in input order. Duplicates (same username, or
    same name case-insensitively) are dropped — first occurrence wins.
    """
    if is_skip(text):
        return []

    members: list[Member] = []
    seen_usernames: set[str] = set()
    seen_names: set[str] = set()

    for raw_line in (text or "").splitlines():
        member = parse_member_line(raw_line)
        if member is None:
            continue
        if member.tg_username and member.tg_username in seen_usernames:
            continue
        if member.name.lower() in seen_names:
            continue
        members.append(member)
        if member.tg_username:
            seen_usernames.add(member.tg_username)
        seen_names.add(member.name.lower())

    return members
