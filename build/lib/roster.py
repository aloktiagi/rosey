"""Single source of truth for parsing the household roster.

Reads `household.md` and returns `Member` objects.

We accept TWO formats so the agent's natural inclination to write either
markdown bullets or tables both work:

  Bullet list (the original format `household.render()` produces):

    # Household

    Members:
    - Alex (tg:123456789) — vegetarian, prefers oat milk
    - Sam (tg:987654321)

  Markdown table (what Claude often writes when asked to populate the
  roster from scratch):

    | Name    | Identifier       | Notes |
    |---------|------------------|-------|
    | Alex    | tg:123456789     | vegetarian |
    | Sam     | tg:987654321     |            |

In either format, members WITHOUT a `tg:`-prefixed identifier (empty
cell or no parens) are silently skipped — they're listed in household.md
for the agent's reference but can't receive Telegram messages.
"""
from __future__ import annotations

import re
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


# Markdown table separator row, e.g. "|---|---|---|" or "| :---: | --- |".
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")


def _parse_bullet_line(line: str) -> Optional[Member]:
    """Match `- Name (tg:NNN) [notes]`. Returns None if the line isn't
    a parseable bullet entry.
    """
    if not line.startswith("- "):
        return None
    rest = line[2:]
    if "(" not in rest or ")" not in rest:
        return None
    name = rest.split("(", 1)[0].strip()
    ident = rest.split("(", 1)[1].split(")", 1)[0].strip()
    notes = rest.split(")", 1)[1].lstrip(" —-:").strip()
    if not name or not ident or not ident.startswith("tg:"):
        return None
    return Member(name=name, identifier=ident, notes=notes)


def _parse_table_row(line: str, header_indices: dict[str, int]) -> Optional[Member]:
    """Parse a `| col1 | col2 | col3 |` row using the column index map
    derived from the table header.
    """
    if "|" not in line:
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if not cells:
        return None
    name_idx = header_indices.get("name")
    ident_idx = header_indices.get("identifier")
    if name_idx is None or ident_idx is None:
        return None
    if name_idx >= len(cells) or ident_idx >= len(cells):
        return None
    name = cells[name_idx]
    ident = cells[ident_idx]
    notes = cells[header_indices["notes"]] if "notes" in header_indices and header_indices["notes"] < len(cells) else ""
    if not name or not ident or not ident.startswith("tg:"):
        return None
    return Member(name=name, identifier=ident, notes=notes)


def members() -> list[Member]:
    """Read household.md and return all parsed members. Empty list if
    the file is missing or has no parseable lines.

    Handles both bullet-list and markdown-table formats. Lines that
    don't match either are silently ignored (headers, prose, blank
    lines, table separators, members without a tg: identifier).
    """
    path = memories_dir() / "household.md"
    if not path.exists():
        return []

    out: list[Member] = []
    # Track the column layout of the current markdown table (if any) so
    # we know which cell holds the name vs identifier vs notes. Reset on
    # blank lines or when a new table starts.
    header_indices: dict[str, int] = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            header_indices = {}
            continue

        # Bullet form takes precedence (and won't match a table row).
        bullet = _parse_bullet_line(stripped)
        if bullet is not None:
            out.append(bullet)
            continue

        # Markdown table parsing. Row types: header, separator, data.
        if "|" in stripped:
            if _MD_TABLE_SEP_RE.match(stripped):
                # Separator row — already saw the header above. Skip.
                continue

            cells = [c.strip().lower() for c in stripped.strip().strip("|").split("|")]
            looks_like_header = (
                "name" in cells
                and any(c in {"identifier", "id", "tg", "chat_id", "telegram"} for c in cells)
            )
            if looks_like_header:
                header_indices = {}
                for i, c in enumerate(cells):
                    if c == "name":
                        header_indices["name"] = i
                    elif c in {"identifier", "id", "tg", "chat_id", "telegram"}:
                        header_indices["identifier"] = i
                    elif c == "notes":
                        header_indices["notes"] = i
                continue

            # Data row — parseable only if we've seen a header in this table.
            if header_indices:
                m = _parse_table_row(stripped, header_indices)
                if m is not None:
                    out.append(m)
                    continue

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
