"""Persistent reminder scheduler.

Replaces the 1-minute polling tick in `reminders.py` with per-reminder,
one-shot DateTrigger jobs backed by a SQLAlchemy jobstore (SQLite). One
reminder = one job that fires at the exact due time. No wasted polls,
sub-second precision, and crash-survival: jobs persist on disk, so a
stop-and-restart reloads them from the jobstore (with `coalesce=True`,
past-due jobs fire exactly once at startup).

Public API:
  - start()              : start the scheduler thread (idempotent).
  - shutdown(wait=False) : stop the scheduler thread (idempotent).
  - reconcile()          : sync /memories/reminders.md → scheduler jobs.
                           Call this on startup AND after each agent turn
                           that may have written to reminders.md.

The agent doesn't talk to this module directly. It writes lines to
reminders.md as before; the reconcile step turns those lines into jobs.
That keeps the file readable/inspectable and avoids tying the agent's
prompt contract to APScheduler internals.

Schema choice: the job ID is sha1(timestamp + message). Stable across
restarts so the same reminder line reconciles to the same job and we
don't double-schedule. When the agent edits or removes a line, the old
job becomes an orphan during reconcile and we delete it.

Malformed lines (timestamp doesn't match LINE_RE) are quarantined into
a `## Malformed` section in reminders.md so they're visible to the
user/agent rather than silently dropped — fixes the "agent says 'I'll
remind you at 8pm' but writes 8:00pm and the reminder never fires" bug.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.date import DateTrigger

import channels
import roster
from paths import memories_dir
from reminder_format import LINE_RE, MENTION_RE

log = logging.getLogger("rosey.scheduler")

_FIRED_HEADER = "## Fired"
_MALFORMED_HEADER = "## Malformed"

# Module-level state. The scheduler is a singleton — there's at most
# one process running per deployment and the jobstore is the source of
# truth, so state-on-disk + state-in-process align trivially.
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


def _db_path() -> Path:
    """Where the SQLite jobstore lives. Defaults to a sibling of the
    memories/ directory so it's easy to back up alongside everything else.
    """
    override = os.environ.get("SCHEDULER_DB_PATH")
    if override:
        return Path(override)
    return memories_dir().parent / "scheduler.db"


def _local_tz():
    tz_name = os.environ.get("SCHEDULER_TZ", "UTC")
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def _task_id(ts_str: str, message: str) -> str:
    """Stable, content-derived job ID. Same line → same ID across restarts,
    so reconcile() doesn't double-schedule. Edited line → new ID, old job
    becomes an orphan and gets removed.
    """
    h = hashlib.sha1(f"{ts_str}|{message}".encode("utf-8")).hexdigest()
    return f"reminder:{h[:16]}"


def start() -> BackgroundScheduler:
    """Start the singleton scheduler. Idempotent — safe to call from
    multiple entry points (telegram_bot.py, app.py).
    """
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            return _scheduler

        db_path = _db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        jobstore = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")

        sched = BackgroundScheduler(
            jobstores={"default": jobstore},
            timezone=_local_tz() or "UTC",
            # coalesce: if multiple fire times have passed (e.g. bot was
            # offline), only fire once. misfire_grace_time: if we restart
            # and a job is past due, fire it within this window. We pick a
            # generous default so reminders that became due while down
            # still go out on resume.
            job_defaults={"coalesce": True, "misfire_grace_time": 24 * 60 * 60},
        )
        sched.start()
        _scheduler = sched
        log.info("scheduler started — jobstore=%s", db_path)
        return sched


def shutdown(wait: bool = False) -> None:
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        try:
            _scheduler.shutdown(wait=wait)
        except Exception:
            log.exception("scheduler shutdown failed")
        _scheduler = None


def fire_reminder(ts_str: str, message: str, recipients: list[str]) -> None:
    """Job target: send the reminder, then move the line to ## Fired in
    reminders.md. Importable as `scheduler.fire_reminder` from the
    APScheduler jobstore (it persists the function path, not the closure).
    """
    if not recipients:
        log.warning("fire_reminder no recipients ts=%s msg=%r", ts_str, message)
        return

    body = f"⏰ Reminder: {message}"
    sent_to: list[str] = []
    for ident in recipients:
        if channels.send(ident, body):
            sent_to.append(ident)

    if not sent_to:
        log.warning("fire_reminder no successful sends ts=%s msg=%r recipients=%s",
                    ts_str, message, recipients)
        # Don't move the line — leave it pending so the next reconcile
        # may retry (e.g. transient Telegram outage).
        return

    log.info("fire_reminder ts=%s msg=%r sent_to=%s", ts_str, message, sent_to)

    # Move the line to ## Fired.
    try:
        _move_to_fired(ts_str, message)
    except Exception:
        log.exception("failed to move fired reminder to ## Fired (ts=%s)", ts_str)


def reconcile() -> None:
    """Sync /memories/reminders.md → scheduler jobs.

    For each pending line that parses against LINE_RE, ensure a one-shot
    DateTrigger job exists. Remove any orphan jobs (whose lines no longer
    appear in pending). Quarantine malformed lines into ## Malformed.

    Idempotent — safe to call repeatedly.
    """
    sched = start()  # ensure running
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")
    head, fired_block, malformed_block = _split_sections(content)

    pending_valid: list[tuple[str, str]] = []   # (ts_str, message)
    new_malformed: list[str] = []
    # kept_head_lines: every original head line that should stay in head —
    # i.e. blanks, headers, AND valid reminder lines. Malformed lines are
    # the only ones moved OUT (into ## Malformed) by the quarantine rewrite.
    kept_head_lines: list[str] = []

    for line in head.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("- "):
            kept_head_lines.append(line)
            continue
        m = LINE_RE.match(line)
        if m:
            ts_str = m.group(1).replace("T", " ")
            message = m.group(2)
            pending_valid.append((ts_str, message))
            kept_head_lines.append(line)
        else:
            new_malformed.append(line)

    # Resolve mentions for each valid line. Names not in household.md
    # mean the reminder gets sent to the whole household instead.
    members_by_name = {m.name.lower(): m.identifier for m in roster.members()}
    all_idents = list(members_by_name.values())

    desired_jobs: dict[str, tuple[str, str, list[str]]] = {}
    for ts_str, message in pending_valid:
        mentions = [n.lower() for n in MENTION_RE.findall(message)]
        clean = MENTION_RE.sub("", message).strip()
        if mentions:
            recipients = [members_by_name[n] for n in mentions if n in members_by_name]
            if not recipients:
                # All mentions unknown — fall back to whole household so
                # the reminder doesn't silently fail to deliver.
                log.warning("reconcile: unknown mentions %s, fanning out to all", mentions)
                recipients = all_idents
        else:
            recipients = all_idents
        if not recipients:
            log.warning("reconcile: no recipients for ts=%s msg=%r — skipping",
                        ts_str, clean)
            continue
        desired_jobs[_task_id(ts_str, message)] = (ts_str, clean, recipients)

    # Sync to APScheduler jobstore.
    existing_ids = {j.id for j in sched.get_jobs() if j.id.startswith("reminder:")}
    desired_ids = set(desired_jobs.keys())

    # Add new jobs.
    tz = _local_tz()
    added = removed = 0
    for jid in desired_ids - existing_ids:
        ts_str, clean_msg, recipients = desired_jobs[jid]
        try:
            due = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
            if tz is not None:
                due = due.replace(tzinfo=tz)
        except ValueError:
            log.warning("reconcile: bad timestamp %r — skipping", ts_str)
            continue
        sched.add_job(
            fire_reminder,
            trigger=DateTrigger(run_date=due),
            args=[ts_str, clean_msg, recipients],
            id=jid,
            replace_existing=True,
        )
        added += 1

    # Remove orphans (jobs whose lines have been edited or deleted).
    for jid in existing_ids - desired_ids:
        try:
            sched.remove_job(jid)
            removed += 1
        except Exception:
            log.exception("reconcile: failed to remove orphan job %s", jid)

    # If we found malformed lines we hadn't seen before, quarantine them.
    # Only rewrite if there's actually a change to make.
    if new_malformed:
        existing_malformed = malformed_block.strip().splitlines() if malformed_block.strip() else []
        existing_malformed_set = set(l.strip() for l in existing_malformed if l.strip().startswith("- "))
        truly_new = [l for l in new_malformed if l.strip() not in existing_malformed_set]
        if truly_new:
            log.warning("reconcile: quarantining %d malformed reminder line(s)", len(truly_new))
            _rewrite_with_quarantine(path, kept_head_lines, fired_block, malformed_block, truly_new)

    if added or removed:
        log.info("reconcile: +%d jobs, -%d orphans (now tracking %d)",
                 added, removed, len(desired_ids))


# ---------------------------------------------------------------------------
# File rewrite helpers — kept here (not in reminders.py) because they all
# coordinate with the scheduler's view of the world.
# ---------------------------------------------------------------------------

def _split_sections(content: str) -> tuple[str, str, str]:
    """Split reminders.md into (head, fired, malformed). Either fired or
    malformed may be empty.
    """
    fired = ""
    malformed = ""
    rest = content

    # ## Malformed first (we put it after ## Fired in the file, but parse
    # in reverse so order doesn't matter).
    if _MALFORMED_HEADER in rest:
        rest, malformed = rest.split(_MALFORMED_HEADER, 1)
    if _FIRED_HEADER in rest:
        rest, fired = rest.split(_FIRED_HEADER, 1)
    return rest, fired, malformed


def _rewrite_with_quarantine(
    path: Path,
    head_lines: list[str],
    fired_block: str,
    malformed_block: str,
    new_malformed: list[str],
) -> None:
    """Move malformed lines from head into ## Malformed. Preserves
    fired_block content.
    """
    parts = []
    head_text = "\n".join(head_lines).rstrip()
    if head_text:
        parts.append(head_text + "\n")

    if fired_block.strip():
        parts.append(f"\n{_FIRED_HEADER}\n{fired_block.strip()}\n")

    combined_malformed_lines = []
    if malformed_block.strip():
        combined_malformed_lines.extend(malformed_block.strip().splitlines())
    combined_malformed_lines.extend(new_malformed)
    if combined_malformed_lines:
        parts.append(
            f"\n{_MALFORMED_HEADER}\n"
            f"(these lines didn't match `[YYYY-MM-DD HH:MM] message @Names` "
            f"and were not scheduled — fix the format and they'll be picked up)\n"
            + "\n".join(l.rstrip() for l in combined_malformed_lines)
            + "\n"
        )

    path.write_text("".join(parts), encoding="utf-8")


def _move_to_fired(ts_str: str, clean_message: str) -> None:
    """Move a successfully-fired line out of head into ## Fired. Matches
    by (timestamp, message) — works whether the original line had @mentions
    that we stripped before passing to fire_reminder.

    Caveat: if the same (timestamp, message) appears twice in pending we
    only move one. That's fine — duplicates would have collapsed to a
    single job ID anyway.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")
    head, fired_block, malformed_block = _split_sections(content)

    new_head_lines = []
    matched = False
    for line in head.splitlines():
        if matched:
            new_head_lines.append(line)
            continue
        m = LINE_RE.match(line)
        if not m:
            new_head_lines.append(line)
            continue
        line_ts = m.group(1).replace("T", " ")
        line_msg = m.group(2)
        # Match by timestamp + message. Compare against both the raw
        # message (with mentions) and the cleaned form, since fire_reminder
        # is called with the cleaned form.
        line_clean = MENTION_RE.sub("", line_msg).strip()
        if line_ts == ts_str and (line_msg.strip() == clean_message or line_clean == clean_message):
            matched = True
            continue  # drop from head
        new_head_lines.append(line)

    if not matched:
        # Already moved or never existed; leave file alone.
        return

    now_str = datetime.now(tz=_local_tz()).strftime("%Y-%m-%d %H:%M") if _local_tz() else \
              datetime.now().strftime("%Y-%m-%d %H:%M")
    fired_entry = f"- [{ts_str}] {clean_message} (fired {now_str})"

    parts = []
    head_text = "\n".join(new_head_lines).rstrip()
    if head_text:
        parts.append(head_text + "\n")

    fired_lines = fired_block.strip().splitlines() if fired_block.strip() else []
    fired_lines.append(fired_entry)
    parts.append(f"\n{_FIRED_HEADER}\n" + "\n".join(fired_lines) + "\n")

    if malformed_block.strip():
        parts.append(f"\n{_MALFORMED_HEADER}\n{malformed_block.strip()}\n")

    path.write_text("".join(parts), encoding="utf-8")
