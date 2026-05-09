"""Single source of truth for parsing the household roster.

Reads `household.md` and returns `Member` objects, one per (name,
identifier) pair. A single person with multiple identifiers (e.g. both
Telegram and WhatsApp) shows up as multiple `Member` rows that share the
same `name` — downstream code groups by name to fan out reminders to
every channel a person uses.

We accept THREE formats so the agent's natural inclination doesn't fight
the parser:

  Multi-identifier bullet (preferred — what the agent tends to produce):

    - **Ankit** — tg:8600355980, wa:+15048755536
    - Sunanda — tg:8637121285

  Single-identifier bullet (legacy `household.render()` output):

    - Alex (tg:123456789) — vegetarian, prefers oat milk

  Markdown table (when an LLM writes the roster from scratch):

    | Name    | Identifier       | Notes      |
    |---------|------------------|------------|
    | Alex    | tg:123456789     | vegetarian |

Lines without any recognizable identifier (e.g. members listed for
reference only — `Mamta`, `Ashok`, `Siya` with empty identifier cells)
are silently skipped. They're in the file for the agent's context but
can't receive messages.

Identifier prefixes recognized: `tg:` (Telegram), `wa:` (WhatsApp),
`alexa:` (Alexa skill user). Adding a new channel is just a regex change
in `_IDENT_RE` below.
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

# Any channel-prefixed identifier we know about. Add new prefixes here
# when wiring a new channel. The pattern is permissive about what comes
# after the prefix:
#   tg:     numeric, optionally negative (group chats)
#   wa:     E.164 with optional leading + and digits only
#   alexa:  amzn1.ask.account.<long alphanumeric blob>
_IDENT_RE = re.compile(
    r"\b(?:"
    r"tg:-?\d+"
    r"|wa:\+?\d+"
    r"|alexa:[A-Za-z0-9_.\-]+"
    r")",
)


def _extract_identifiers(line: str) -> list[str]:
    """Return all channel-prefixed identifiers found in the line, in
    order of appearance. Lowercased for tg/wa (chat IDs are case-
    insensitive); alexa: ids preserve case (Amazon's amzn1.ask.account.X
    is sometimes uppercase)."""
    out = []
    for raw in _IDENT_RE.findall(line):
        if raw.startswith(("tg:", "wa:")):
            out.append(raw)
        else:
            out.append(raw)  # preserve case for alexa
    return out


def _extract_name(line: str) -> str:
    """Strip identifiers, markdown noise, and separators from a line to
    isolate just the person's name. Used by the flexible parser for
    free-form bullets like:
        - **Ankit** — tg:..., wa:...
        - Sunanda — tg:...
        - Sunanda (tg:...) — vegetarian
    """
    s = _IDENT_RE.sub("", line)
    s = s.replace("**", "").replace("*", "")
    s = s.lstrip("-•*| \t")
    # Cut at the FIRST natural separator (em dash, paren, pipe, colon).
    # The agent uses ` — ` consistently in the multi-id format, but
    # we accept all common ones.
    for sep in ("—", "–", "(", "|", ":"):
        idx = s.find(sep)
        if idx >= 0:
            s = s[:idx]
            break
    # Strip trailing punctuation (commas left by removed identifiers,
    # stray dashes from where the identifier list lived).
    return s.strip(" ,—–|()*\t")


def _parse_flexible_bullet(line: str) -> list[Member]:
    """Parse a bullet line with one OR MORE identifiers into a list of
    Member objects sharing the same name. Returns [] if no identifier
    found (line is informational, e.g. "- Mamta" with no tg/wa).

    Handles all of these:
        - **Ankit** — tg:8600355980, wa:+15048755536
        - Sunanda — tg:8637121285
        - Alex (tg:123456789) — vegetarian
        * Sam: tg:987654321
    """
    if not (line.startswith("- ") or line.startswith("* ") or line.startswith("• ")):
        return []
    idents = _extract_identifiers(line)
    if not idents:
        return []
    name = _extract_name(line)
    if not name:
        return []
    return [Member(name=name, identifier=ident, notes="") for ident in idents]


def _parse_bullet_line(line: str) -> Optional[Member]:
    """Legacy single-identifier bullet parser, kept for files that still
    use the original `- Name (tg:NNN) [notes]` format with notes preserved.
    The flexible parser handles the same lines too but throws away notes;
    when a line matches this stricter pattern we prefer it for note fidelity.
    """
    if not line.startswith("- "):
        return None
    rest = line[2:]
    if "(" not in rest or ")" not in rest:
        return None
    name = rest.split("(", 1)[0].strip()
    ident = rest.split("(", 1)[1].split(")", 1)[0].strip()
    notes = rest.split(")", 1)[1].lstrip(" —-:").strip()
    if not name or not ident or not _IDENT_RE.fullmatch(ident):
        return None
    return Member(name=name, identifier=ident, notes=notes)


def _parse_table_row(line: str, header_indices: dict[str, int]) -> list[Member]:
    """Parse a `| col1 | col2 | col3 |` row using the column index map
    derived from the table header. Returns one Member per identifier
    (table cell may contain comma-separated identifiers like
    `tg:..., wa:...`).
    """
    if "|" not in line:
        return []
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if not cells:
        return []
    name_idx = header_indices.get("name")
    ident_idx = header_indices.get("identifier")
    if name_idx is None or ident_idx is None:
        return []
    if name_idx >= len(cells) or ident_idx >= len(cells):
        return []
    name = cells[name_idx]
    ident_cell = cells[ident_idx]
    notes = cells[header_indices["notes"]] if "notes" in header_indices and header_indices["notes"] < len(cells) else ""
    if not name:
        return []
    idents = _extract_identifiers(ident_cell)
    if not idents:
        return []
    return [Member(name=name, identifier=ident, notes=notes) for ident in idents]


def members() -> list[Member]:
    """Read household.md and return all parsed members. Empty list if
    the file is missing or has no parseable lines.

    Handles bullet-list (multi- or single-identifier) and markdown-table
    formats. Lines that don't match are silently ignored (headers, prose,
    blank lines, table separators, members without any identifier).
    """
    path = memories_dir() / "household.md"
    if not path.exists():
        return []

    out: list[Member] = []
    # Track the column layout of the current markdown table (if any).
    header_indices: dict[str, int] = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            header_indices = {}
            continue

        # Try the legacy single-identifier bullet first — preserves notes
        # on lines that match the strict `- Name (tg:NNN) — notes` shape.
        legacy = _parse_bullet_line(stripped)
        if legacy is not None:
            out.append(legacy)
            continue

        # Then the flexible bullet parser (handles multi-identifier and
        # the agent's preferred `- **Name** — id1, id2` format).
        flex = _parse_flexible_bullet(stripped)
        if flex:
            out.extend(flex)
            continue

        # Finally, markdown table rows.
        if "|" in stripped:
            if _MD_TABLE_SEP_RE.match(stripped):
                continue

            cells = [c.strip().lower() for c in stripped.strip().strip("|").split("|")]
            looks_like_header = (
                "name" in cells
                and any(
                    c in {"identifier", "identifiers", "id", "tg", "chat_id", "telegram"}
                    for c in cells
                )
            )
            if looks_like_header:
                header_indices = {}
                for i, c in enumerate(cells):
                    if c == "name":
                        header_indices["name"] = i
                    elif c in {"identifier", "identifiers", "id", "tg", "chat_id", "telegram"}:
                        header_indices["identifier"] = i
                    elif c == "notes":
                        header_indices["notes"] = i
                continue

            if header_indices:
                rows = _parse_table_row(stripped, header_indices)
                if rows:
                    out.extend(rows)
                    continue

    return out


def members_grouped_by_name() -> dict[str, list[str]]:
    """Convenience: { name_lowercased: [identifier, ...] }. Used by the
    scheduler to fan out a single @-mention to every channel that person
    uses.
    """
    grouped: dict[str, list[str]] = {}
    for m in members():
        grouped.setdefault(m.name.lower(), []).append(m.identifier)
    return grouped


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
