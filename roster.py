"""Single source of truth for parsing the household roster.

Reads `household.md` and returns `Member` objects. Replaces three
slightly-different parsers that were drifting (in `household.py`,
`reminders.py`, and `telegram_bot.py`).

The roster file lives at `<MEMORY_ROOT>/memories/household.md` and
follows the format `render()` writes:

    # Household

    Members:
    - Alex (tg:123456789) — vegetarian, prefers oat milk
    - Sam (tg:987654321)

    Shopping cadence: weekly
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from paths import memories_dir


@dataclass(frozen=True)
class Member:
    name: str
    identifier: str   # e.g. "tg:NNN"
    notes: str = ""

    @property
    def is_telegram(self) -> bool:
        return self.identifier.startswith("tg:")

    @property
    def telegram_chat_id(self) -> Optional[int]:
        if not self.is_telegram:
            return None
        try:
            return int(self.identifier[len("tg:"):])
        except ValueError:
            return None


def members() -> list[Member]:
    """Read household.md and return all parsed members. Empty list if
    the file is missing or has no parseable lines."""
    path = memories_dir() / "household.md"
    if not path.exists():
        return []
    out: list[Member] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        rest = line[2:]
        if "(" not in rest or ")" not in rest:
            continue
        name = rest.split("(", 1)[0].strip()
        ident = rest.split("(", 1)[1].split(")", 1)[0].strip()
        notes = rest.split(")", 1)[1].lstrip(" —-:").strip()
        if not name or not ident:
            continue
        if ident.startswith("tg:"):
            out.append(Member(name=name, identifier=ident, notes=notes))
    return out


def by_name(name: str) -> Optional[Member]:
    """Lookup a member by case-insensitive name match."""
    lower = name.lower()
    for m in members():
        if m.name.lower() == lower:
            return m
    return None


def by_identifier(identifier: str) -> Optional[Member]:
    for m in members():
        if m.identifier == identifier:
            return m
    return None


def telegram_chat_ids() -> set[int]:
    """All Telegram chat_ids in the roster, as ints."""
    return {m.telegram_chat_id for m in members() if m.telegram_chat_id is not None}


def is_authorized(identifier: str) -> bool:
    """If the roster is empty (initial setup), trust everyone. Once it
    has any entries, only listed identifiers are authorized."""
    ms = members()
    if not ms:
        return True
    return any(m.identifier == identifier for m in ms)
