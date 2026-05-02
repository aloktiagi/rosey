"""Real-time-ish reminder polling.

The agent writes due-items to /memories/reminders.md in this exact format:

    - [YYYY-MM-DD HH:MM] message @Name1 @Name2

A scheduler tick runs check_due() every minute. For each entry whose due
time has passed, we send the message to the matching members via
`channels.send` (looked up from household.md), then move the line into a
"## Fired" section so it doesn't fire again.

Format choice: structured timestamps + @mentions are cheap to parse and
hard for the LLM to mess up. Free-form natural-language dates would
constantly drift.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

import channels
import roster
from paths import memories_dir
from reminder_format import LINE_RE, MENTION_RE

log = logging.getLogger("rosey.reminders")


def _name_to_identifier() -> dict:
    """{name_lowercase: identifier} for @mention lookups."""
    return {m.name.lower(): m.identifier for m in roster.members()}


def _now_local() -> datetime:
    tz_name = os.environ.get("SCHEDULER_TZ", "UTC")
    tz = ZoneInfo(tz_name) if ZoneInfo else None
    return datetime.now(tz=tz)


# Outbound dispatch lives in `channels`. Re-exported under the previous
# private name so the rest of this module's flow reads naturally.
_send_reminder = channels.send


def check_due() -> None:
    """Read reminders.md, fire any due items, persist the move."""
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")
    if not content.strip():
        return

    tz_name = os.environ.get("SCHEDULER_TZ", "UTC")
    tz = ZoneInfo(tz_name) if ZoneInfo else None
    now = _now_local()

    members = _name_to_identifier()

    # Split off any existing "## Fired" tail; we re-attach later.
    if "## Fired" in content:
        head, fired_block = content.split("## Fired", 1)
    else:
        head, fired_block = content, ""

    pending_lines = head.splitlines()
    new_pending: list = []
    new_fired: list = []

    for line in pending_lines:
        m = LINE_RE.match(line)
        if not m:
            new_pending.append(line)
            continue

        ts_str = m.group(1).replace("T", " ")
        message = m.group(2)

        try:
            due = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
            if tz is not None:
                due = due.replace(tzinfo=tz)
        except ValueError:
            new_pending.append(line)
            continue

        if due > now:
            new_pending.append(line)
            continue

        # Item is due.
        mentions = [n.lower() for n in MENTION_RE.findall(message)]
        clean = MENTION_RE.sub("", message).strip()

        if mentions:
            recipients = [members[n] for n in mentions if n in members]
            missing = [n for n in mentions if n not in members]
            if missing:
                log.warning("reminder mentions unknown names: %s", missing)
        else:
            recipients = list(members.values())

        if not recipients:
            log.warning("reminder %r had no resolvable recipients; keeping for retry", line)
            new_pending.append(line)
            continue

        body = f"⏰ Reminder: {clean}"
        sent_any = False
        for ident in recipients:
            if _send_reminder(ident, body):
                sent_any = True

        if sent_any:
            new_fired.append(
                f"- [{ts_str}] {clean} (fired {now.strftime('%Y-%m-%d %H:%M')})"
            )
            log.info("fired reminder: %r → %d recipient(s)", clean, len(recipients))
        else:
            # Couldn't send to anyone — keep it for retry next minute.
            new_pending.append(line)

    if not new_fired:
        return  # no changes

    head_text = "\n".join(new_pending).rstrip()
    if not head_text.endswith("\n"):
        head_text += "\n"

    fired_section = "\n## Fired\n" + (fired_block.strip() + "\n" if fired_block.strip() else "")
    fired_section += "\n".join(new_fired) + "\n"

    path.write_text(head_text + fired_section, encoding="utf-8")
