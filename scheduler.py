"""Persistent reminder scheduler with three-stage lifecycle.

Each pending reminder line in /memories/reminders.md gets THREE one-shot
DateTrigger jobs in the SQLAlchemy-backed jobstore:

    fire:<id>      — initial ping at T, sent to the @-named recipients
    escalate:<id>  — at T + esc, fans out to the originating chat if no ack
    miss:<id>      — at T + miss, marks the reminder missed if still no ack

The escalation lifecycle is encoded as annotations the line accumulates over
time (`(fired at ...)`, `(escalated to ...)`, `(acked by ...)`, `(missed at
...)`). Each job re-reads the line at fire time and self-skips if a later
state is already present, so we don't need explicit job cancellation on
ack — robust to crashes, file edits, and out-of-order delivery.

Default cadence (used when the agent doesn't specify `esc:` / `miss:` tags
on the line):
    esc:  30 minutes after primary fire
    miss: 24 hours  after primary fire

The agent can override per-task with `esc:5m miss:1h` (urgent), `esc:1d
miss:7d` (slow burn), etc.

Public API:
    start()                   start the singleton scheduler (idempotent)
    shutdown()                stop it (idempotent)
    reconcile()               sync reminders.md → scheduler jobs; call on
                              startup and after each agent turn that may
                              have written reminders.md
    mark_acked(task_id, by)   record an ack annotation on the matching
                              line. Future escalate/miss runs self-skip.
    find_task_by_chat_msg(c,m) look up which task_id (if any) sent the
                              Telegram message at (chat_id, msg_id), used
                              by reply-to-bot ack detection
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

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
from reminder_format import (
    ACKED_RE,
    ESC_RE,
    FROM_RE,
    ID_RE,
    LINE_RE,
    MENTION_RE,
    MISS_RE,
)

log = logging.getLogger("rosey.scheduler")

# Section headers in reminders.md. Order in the file: head → Fired →
# Missed → Malformed → Failed_Delivery.
_FIRED_HEADER = "## Fired"
_MISSED_HEADER = "## Missed"
_MALFORMED_HEADER = "## Malformed"
_FAILED_HEADER = "## Failed_Delivery"

# Default escalation cadences (used when esc:/miss: tags absent).
DEFAULT_ESCALATE_AFTER = timedelta(minutes=30)
DEFAULT_MISS_AFTER = timedelta(hours=24)

# Module-level singleton scheduler.
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """Where the SQLite jobstore lives. Defaults to a sibling of memories/
    so it backs up alongside everything else.
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


def _now_str() -> str:
    tz = _local_tz()
    return datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M")


def _generate_id(ts_str: str, message: str) -> str:
    """12-char content-derived id, stable across restarts. Same line →
    same id → reconcile is idempotent. Edited line → new id, old jobs
    age out as orphans on next reconcile.
    """
    return hashlib.sha1(f"{ts_str}|{message}".encode("utf-8")).hexdigest()[:12]


def _parse_duration(value: str, unit: str) -> timedelta:
    n = int(value)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    raise ValueError(f"unknown duration unit: {unit!r}")


def _strip_to_user_message(message: str) -> str:
    """Remove all metadata tags + parenthetical annotations to get the
    plain message body suitable for sending in a Telegram reminder.
    """
    s = message
    s = MENTION_RE.sub("", s)
    s = FROM_RE.sub("", s)
    s = ID_RE.sub("", s)
    s = ESC_RE.sub("", s)
    s = MISS_RE.sub("", s)
    # Strip parenthetical lifecycle annotations: (fired ...), (acked ...) etc.
    s = re.sub(r"\([^)]*\)", "", s)
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Lifecycle: start / shutdown
# ---------------------------------------------------------------------------

def start() -> BackgroundScheduler:
    """Start the singleton scheduler. Idempotent."""
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
            # If the bot was offline when a job was due, fire it within
            # this window on resume. coalesce → only once even if many
            # ticks were missed.
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


# ---------------------------------------------------------------------------
# File parsing / rewriting
# ---------------------------------------------------------------------------

def _split_sections(content: str) -> dict:
    """Parse reminders.md into its named sections.

    Returns a dict with keys: head, fired, missed, malformed, failed.
    Order in the file is parsed in reverse so relative ordering doesn't
    matter — but on rewrite we always emit in the canonical order.
    """
    fired = ""
    missed = ""
    malformed = ""
    failed = ""
    rest = content
    if _FAILED_HEADER in rest:
        rest, failed = rest.split(_FAILED_HEADER, 1)
    if _MALFORMED_HEADER in rest:
        rest, malformed = rest.split(_MALFORMED_HEADER, 1)
    if _MISSED_HEADER in rest:
        rest, missed = rest.split(_MISSED_HEADER, 1)
    if _FIRED_HEADER in rest:
        rest, fired = rest.split(_FIRED_HEADER, 1)
    return {
        "head": rest,
        "fired": fired,
        "missed": missed,
        "malformed": malformed,
        "failed": failed,
    }


def _rewrite_file(
    path: Path,
    head_lines: list[str],
    fired_lines: list[str],
    missed_lines: list[str],
    malformed_block: str,
    failed_block: str,
    new_malformed: list[str] | None = None,
    new_failed: list[tuple[str, str]] | None = None,
) -> None:
    """Atomically rewrite reminders.md with the canonical section order.

    `head_lines`, `fired_lines`, `missed_lines` are the desired post-rewrite
    contents of each section (callers manage the membership). `malformed_block`
    and `failed_block` are passed through verbatim, optionally with new
    entries appended via `new_malformed` / `new_failed`.
    """
    parts: list[str] = []

    head_text = "\n".join(head_lines).rstrip()
    if head_text:
        parts.append(head_text + "\n")

    if fired_lines:
        parts.append(f"\n{_FIRED_HEADER}\n" + "\n".join(fired_lines) + "\n")

    if missed_lines:
        parts.append(f"\n{_MISSED_HEADER}\n" + "\n".join(missed_lines) + "\n")

    combined_malformed: list[str] = []
    if malformed_block.strip():
        combined_malformed.extend(malformed_block.strip().splitlines())
    if new_malformed:
        combined_malformed.extend(new_malformed)
    if combined_malformed:
        parts.append(
            f"\n{_MALFORMED_HEADER}\n"
            "(these lines didn't match `[YYYY-MM-DD HH:MM] message ...` "
            "and were not scheduled — fix the format and they'll be picked up)\n"
            + "\n".join(l.rstrip() for l in combined_malformed)
            + "\n"
        )

    combined_failed: list[str] = []
    if failed_block.strip():
        combined_failed.extend(failed_block.strip().splitlines())
    if new_failed:
        for raw_line, reason in new_failed:
            combined_failed.append(f"{raw_line.rstrip()}  ⚠️ {reason}")
    if combined_failed:
        parts.append(
            f"\n{_FAILED_HEADER}\n"
            "(these reminders parsed correctly but had no resolvable "
            "recipients — list members in /memories/household.md or include "
            "a `from:tg:<chat_id>` tag, then move the line back above)\n"
            + "\n".join(combined_failed)
            + "\n"
        )

    path.write_text("".join(parts), encoding="utf-8")


def _ensure_id(line: str, ts_str: str, raw_message: str) -> tuple[str, str, bool]:
    """Return (line_with_id, task_id, mutated). If the line lacked an
    `id:<hex>` tag, append one and report mutated=True so the caller
    rewrites the file.
    """
    found = ID_RE.findall(raw_message)
    if found:
        return line, found[0], False
    task_id = _generate_id(ts_str, raw_message)
    new_line = f"{line.rstrip()} id:{task_id}"
    return new_line, task_id, True


def _line_for_task(task_id: str, sections: dict) -> Optional[tuple[str, str]]:
    """Find the line with `id:<task_id>` in any section. Returns
    (section_name, line) or None.
    """
    for section in ("head", "fired", "missed"):
        for line in sections[section].splitlines():
            if not line.lstrip().startswith("- "):
                continue
            ids = ID_RE.findall(line)
            if task_id in ids:
                return section, line
    return None


def _replace_line_in_section(
    section_text: str, old_line: str, new_line: str | None
) -> str:
    """Replace `old_line` in a section. If `new_line` is None, drop it."""
    out = []
    replaced = False
    for line in section_text.splitlines():
        if not replaced and line == old_line:
            replaced = True
            if new_line is not None:
                out.append(new_line)
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Job targets — fire / escalate / miss
# ---------------------------------------------------------------------------

def fire_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    recipients: list[str],
    origin_chat: str,
) -> None:
    """Primary fire: send to recipients, capture msg_ids, move line from
    head to ## Fired with annotation.
    """
    if not recipients:
        log.warning("fire_one task=%s: no recipients, noop", task_id)
        return

    body_text = _strip_to_user_message(raw_message)
    body = f"⏰ Reminder: {body_text}"

    sent_pairs: list[tuple[str, int]] = []
    for ident in recipients:
        msg_id = channels.send_returning_msg_id(ident, body)
        if msg_id is not None:
            sent_pairs.append((ident, msg_id))

    if not sent_pairs:
        log.warning("fire_one task=%s: no successful sends, leaving pending", task_id)
        return

    log.info("fire_one task=%s sent_to=%s", task_id, [p[0] for p in sent_pairs])

    msg_pairs_str = " ".join(f"chat:{ident} msg:{mid}" for ident, mid in sent_pairs)
    annotation = f"(fired at {_now_str()} {msg_pairs_str})"
    _annotate_and_move(task_id, annotation, target_section="fired")


def escalate_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    origin_chat: str,
) -> None:
    """If the task hasn't been acked yet, escalate to the originating
    chat (group). Self-skip if already acked or already escalated.
    """
    state = _read_task_state(task_id)
    if state is None:
        log.info("escalate_one task=%s: line not found, noop", task_id)
        return
    if state["acked"] or state["missed"]:
        log.info("escalate_one task=%s: already %s, noop",
                 task_id, "acked" if state["acked"] else "missed")
        return
    if state["escalated"]:
        log.info("escalate_one task=%s: already escalated, noop", task_id)
        return

    body_text = _strip_to_user_message(raw_message)
    body = f"⏰ Reminder (re-ping, no ack yet): {body_text}"

    msg_id = channels.send_returning_msg_id(origin_chat, body)
    if msg_id is None:
        log.warning("escalate_one task=%s: send to %s failed", task_id, origin_chat)
        return

    log.info("escalate_one task=%s sent_to=%s msg=%s", task_id, origin_chat, msg_id)
    annotation = f"(escalated to chat:{origin_chat} msg:{msg_id} at {_now_str()})"
    _append_annotation(task_id, annotation)


def miss_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    origin_chat: str,
) -> None:
    """If still un-acked at the miss horizon, move to ## Missed and notify."""
    state = _read_task_state(task_id)
    if state is None:
        return
    if state["acked"] or state["missed"]:
        return

    body_text = _strip_to_user_message(raw_message)
    body = f"⚠️ Missed reminder (no acknowledgement): {body_text}"
    channels.send_returning_msg_id(origin_chat, body)

    log.info("miss_one task=%s — moved to ## Missed", task_id)
    annotation = f"(missed at {_now_str()} — no ack)"
    _annotate_and_move(task_id, annotation, target_section="missed")


# ---------------------------------------------------------------------------
# Ack helpers — called from telegram_bot for reply-to-bot acks, and indirectly
# (via agent annotation) for natural-language acks.
# ---------------------------------------------------------------------------

def mark_acked(task_id: str, by_name: str) -> bool:
    """Append `(acked by <name> at <now>)` to the task's line. Returns True
    if the line was found and annotated, False otherwise. Future escalate/
    miss runs will self-skip on the annotation.
    """
    annotation = f"(acked by {by_name} at {_now_str()})"
    return _append_annotation(task_id, annotation)


def find_task_by_chat_msg(chat_id: str, msg_id: int) -> Optional[str]:
    """For reply-to-bot ack lookup. Scan reminders.md's annotations for a
    matching `chat:<chat_id> msg:<msg_id>` token. Returns the task_id of
    the line containing it, or None.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return None
    needle = f"chat:{chat_id} msg:{msg_id}"
    for line in path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            ids = ID_RE.findall(line)
            if ids:
                return ids[0]
    return None


def _read_task_state(task_id: str) -> Optional[dict]:
    """Return state dict for the task or None if no line found. Searches
    head, ## Fired, and ## Missed sections.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return None
    sections = _split_sections(path.read_text(encoding="utf-8"))
    found = _line_for_task(task_id, sections)
    if found is None:
        return None
    section_name, line = found
    return {
        "section": section_name,
        "line": line,
        "acked": bool(ACKED_RE.search(line)),
        "escalated": "(escalated" in line,
        "missed": section_name == "missed" or "(missed" in line,
    }


def _append_annotation(task_id: str, annotation: str) -> bool:
    """In-place: append `annotation` to the task's line, regardless of
    which section it's in. Used for ack and escalation markers.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    sections = _split_sections(content)
    found = _line_for_task(task_id, sections)
    if found is None:
        return False
    section_name, line = found
    new_line = f"{line.rstrip()} {annotation}"
    sections[section_name] = _replace_line_in_section(sections[section_name], line, new_line)

    head_lines = sections["head"].splitlines()
    fired_lines = [l for l in sections["fired"].splitlines() if l.strip()]
    missed_lines = [l for l in sections["missed"].splitlines() if l.strip()]
    _rewrite_file(
        path, head_lines, fired_lines, missed_lines,
        sections["malformed"], sections["failed"],
    )
    return True


def _annotate_and_move(task_id: str, annotation: str, target_section: str) -> bool:
    """Append `annotation` to the line and move it to `target_section`
    (one of "fired" or "missed"). The line might currently be in head
    (initial fire) or fired (miss after escalate). Idempotent: if the line
    is already in target_section we just append the annotation in place.
    """
    if target_section not in ("fired", "missed"):
        raise ValueError(f"invalid target_section: {target_section}")

    path = memories_dir() / "reminders.md"
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    sections = _split_sections(content)
    found = _line_for_task(task_id, sections)
    if found is None:
        return False

    current_section, line = found
    new_line = f"{line.rstrip()} {annotation}"

    if current_section == target_section:
        # Just annotate in place.
        sections[current_section] = _replace_line_in_section(
            sections[current_section], line, new_line,
        )
    else:
        # Remove from current section, append to target section.
        sections[current_section] = _replace_line_in_section(
            sections[current_section], line, None,
        )
        if sections[target_section].strip():
            sections[target_section] = sections[target_section].rstrip() + "\n" + new_line
        else:
            sections[target_section] = new_line

    head_lines = sections["head"].splitlines()
    fired_lines = [l for l in sections["fired"].splitlines() if l.strip()]
    missed_lines = [l for l in sections["missed"].splitlines() if l.strip()]
    _rewrite_file(
        path, head_lines, fired_lines, missed_lines,
        sections["malformed"], sections["failed"],
    )
    return True


# ---------------------------------------------------------------------------
# Reconcile: the entry point called on startup and after each agent turn.
# ---------------------------------------------------------------------------

def reconcile() -> None:
    """Sync /memories/reminders.md → scheduler jobs.

    For each pending head line:
      - assign an `id:` if missing (rewriting the file)
      - resolve recipients via @-mentions → from: tag → all members cascade
      - register fire/escalate/miss jobs (idempotent, replace_existing)
      - if no recipients, move to ## Failed_Delivery

    Malformed lines (don't match LINE_RE) → ## Malformed.
    Stale jobs whose lines are gone or already acked → removed as orphans.
    """
    sched = start()
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")
    sections = _split_sections(content)
    head = sections["head"]
    fired_block = sections["fired"]
    missed_block = sections["missed"]
    malformed_block = sections["malformed"]
    failed_block = sections["failed"]

    pending: list[dict] = []     # parsed valid pending lines
    new_malformed: list[str] = []
    kept_head_lines: list[str] = []
    file_mutated = False

    for raw_line in head.splitlines():
        stripped = raw_line.strip()
        if not stripped or not stripped.startswith("- "):
            kept_head_lines.append(raw_line)
            continue
        m = LINE_RE.match(raw_line)
        if not m:
            new_malformed.append(raw_line)
            continue
        ts_str = m.group(1).replace("T", " ")
        message = m.group(2)
        # Skip lines that are already acked (a natural-language ack
        # annotation may have been added by the agent).
        if ACKED_RE.search(raw_line):
            kept_head_lines.append(raw_line)
            continue
        # Ensure id present, mutating the line if needed.
        canonical_line, task_id, mutated = _ensure_id(raw_line, ts_str, message)
        if mutated:
            file_mutated = True
            # Re-extract message from the mutated line so it includes id:
            mm = LINE_RE.match(canonical_line)
            message = mm.group(2) if mm else message
        kept_head_lines.append(canonical_line)
        pending.append({
            "task_id": task_id,
            "ts_str": ts_str,
            "raw_message": message,
            "raw_line": canonical_line,
        })

    # Recipient cascade + per-line escalation cadences.
    members_by_name = {m.name.lower(): m.identifier for m in roster.members()}
    all_idents = list(members_by_name.values())
    failed_to_deliver: list[tuple[str, str, str]] = []

    desired_jobs: dict[str, dict] = {}
    for p in pending:
        msg = p["raw_message"]
        mentions = [n.lower() for n in MENTION_RE.findall(msg)]
        from_chats = FROM_RE.findall(msg)

        if mentions:
            recipients = [members_by_name[n] for n in mentions if n in members_by_name]
            if not recipients:
                log.warning(
                    "reconcile: unknown mentions %s, falling back to from:%s or all",
                    mentions, from_chats,
                )
                recipients = from_chats or all_idents
        else:
            recipients = all_idents or from_chats

        if not recipients:
            reason = (
                "no @-mentions resolved and no recipients available "
                "(household.md is empty and no from: tag)"
            )
            log.warning(
                "reconcile: failed delivery ts=%s msg=%r — %s",
                p["ts_str"], _strip_to_user_message(msg), reason,
            )
            failed_to_deliver.append((p["task_id"], p["raw_line"], reason))
            continue

        # Pick origin chat: prefer from: tag, else first recipient.
        origin_chat = from_chats[0] if from_chats else recipients[0]

        # Per-line cadence overrides.
        esc_match = ESC_RE.search(msg)
        miss_match = MISS_RE.search(msg)
        try:
            esc_delta = (
                _parse_duration(*esc_match.groups()) if esc_match
                else DEFAULT_ESCALATE_AFTER
            )
        except ValueError:
            esc_delta = DEFAULT_ESCALATE_AFTER
        try:
            miss_delta = (
                _parse_duration(*miss_match.groups()) if miss_match
                else DEFAULT_MISS_AFTER
            )
        except ValueError:
            miss_delta = DEFAULT_MISS_AFTER

        desired_jobs[p["task_id"]] = {
            "ts_str": p["ts_str"],
            "raw_message": msg,
            "recipients": recipients,
            "origin_chat": origin_chat,
            "esc_delta": esc_delta,
            "miss_delta": miss_delta,
        }

    # Remove failed-to-deliver lines from head so the rewrite drops them.
    truly_new_failed: list[tuple[str, str]] = []
    if failed_to_deliver:
        existing_failed = failed_block.strip().splitlines() if failed_block.strip() else []
        existing_failed_set = {
            l.strip() for l in existing_failed if l.strip().startswith("- ")
        }
        for tid, raw_line, reason in failed_to_deliver:
            if not raw_line or raw_line.strip() in existing_failed_set:
                continue
            try:
                kept_head_lines.remove(raw_line)
            except ValueError:
                pass
            truly_new_failed.append((raw_line, reason))

    # Sync APScheduler jobstore.
    tz = _local_tz()
    desired_job_ids: set[str] = set()
    added = 0
    for tid, info in desired_jobs.items():
        try:
            due = datetime.strptime(info["ts_str"], "%Y-%m-%d %H:%M")
            if tz is not None:
                due = due.replace(tzinfo=tz)
        except ValueError:
            log.warning("reconcile: bad timestamp %r — skipping", info["ts_str"])
            continue

        fire_id = f"fire:{tid}"
        esc_id = f"escalate:{tid}"
        miss_id = f"miss:{tid}"
        desired_job_ids.update({fire_id, esc_id, miss_id})

        sched.add_job(
            fire_one,
            trigger=DateTrigger(run_date=due),
            args=[tid, info["ts_str"], info["raw_message"],
                  info["recipients"], info["origin_chat"]],
            id=fire_id,
            replace_existing=True,
        )
        sched.add_job(
            escalate_one,
            trigger=DateTrigger(run_date=due + info["esc_delta"]),
            args=[tid, info["ts_str"], info["raw_message"], info["origin_chat"]],
            id=esc_id,
            replace_existing=True,
        )
        sched.add_job(
            miss_one,
            trigger=DateTrigger(run_date=due + info["miss_delta"]),
            args=[tid, info["ts_str"], info["raw_message"], info["origin_chat"]],
            id=miss_id,
            replace_existing=True,
        )
        added += 1

    # Orphans: jobs in store that aren't desired (line edited or removed)
    # AND legacy "reminder:" prefix jobs from the previous scheduler version.
    removed = 0
    for j in sched.get_jobs():
        if j.id.startswith("reminder:") or (
            j.id.startswith(("fire:", "escalate:", "miss:"))
            and j.id not in desired_job_ids
        ):
            try:
                sched.remove_job(j.id)
                removed += 1
            except Exception:
                log.exception("reconcile: failed to remove orphan %s", j.id)

    # Rewrite reminders.md if anything changed (id: insertion, quarantines).
    if file_mutated or new_malformed or truly_new_failed:
        # Filter new_malformed against existing malformed to stay idempotent.
        existing_malformed = malformed_block.strip().splitlines() if malformed_block.strip() else []
        existing_malformed_set = {
            l.strip() for l in existing_malformed if l.strip().startswith("- ")
        }
        truly_new_malformed = [
            l for l in new_malformed if l.strip() not in existing_malformed_set
        ]

        fired_lines = [l for l in fired_block.splitlines() if l.strip()]
        missed_lines = [l for l in missed_block.splitlines() if l.strip()]
        _rewrite_file(
            path,
            kept_head_lines,
            fired_lines,
            missed_lines,
            malformed_block,
            failed_block,
            new_malformed=truly_new_malformed,
            new_failed=truly_new_failed,
        )

    if added or removed:
        log.info(
            "reconcile: +%d reminders (×3 jobs each = %d), -%d orphans",
            added, added * 3, removed,
        )


# ---------------------------------------------------------------------------
# Compatibility shim — old jobstore entries from before the lifecycle rewrite
# referenced this function. They get cleaned up on first reconcile (see
# orphan removal above), but keep the symbol importable so APScheduler can
# load and immediately delete them without ImportError.
# ---------------------------------------------------------------------------

def fire_reminder(*args, **kwargs) -> None:  # pragma: no cover
    log.info("legacy fire_reminder shim invoked — should be cleaned up on next reconcile")
