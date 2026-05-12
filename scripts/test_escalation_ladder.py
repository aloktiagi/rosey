"""Smoke tests for the per-addressee escalation ladder.

Run from the repo root:

    PYTHONPATH=. python scripts/test_escalation_ladder.py

Exercises the public scheduler API (reconcile + the four job targets)
against a temp /memories dir, with channels.send_returning_msg_id
patched to capture calls instead of hitting real APIs.

Cases:
  1. urg:high schedules fast intervals + fans per-addressee
  2. urg:low only schedules fire + miss (no escalate, no fallback)
  3. ack on the line cancels every pending tier (escalate self-skips)
  4. fallback resolves the dynamic recipient correctly (not an addressee)
  5. cross-channel ack works (Sunanda acks via wa for a tg-fired reminder)
  6. snooze pattern: ack + new line both reconcile cleanly

Output: prints "PASS" / "FAIL" lines and exits 1 on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


# Ensure repo root on sys.path when invoked as a script.
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# Each test creates an isolated temp dir and points MEMORY_ROOT at it.
# scheduler.py reads from paths.memories_dir() which respects MEMORY_ROOT.

FAILURES: list[str] = []


def _setup_temp_memory(roster_md: str) -> Path:
    """Create a fresh /memories dir with household.md and an empty
    reminders.md. Returns the dir path."""
    tmp = Path(tempfile.mkdtemp(prefix="rosey-sched-test-"))
    mem = tmp / "memories"
    mem.mkdir()
    (mem / "household.md").write_text(roster_md, encoding="utf-8")
    (mem / "reminders.md").write_text("", encoding="utf-8")
    os.environ["MEMORY_ROOT"] = str(tmp)
    os.environ["SCHEDULER_DB_PATH"] = str(tmp / "scheduler.db")
    os.environ["SCHEDULER_TZ"] = "UTC"
    return tmp


def _check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        msg = f"  FAIL  {label}" + (f"  ({detail})" if detail else "")
        print(msg)
        FAILURES.append(label)


def _reset_modules() -> None:
    """Drop scheduler / paths / roster from sys.modules so each test
    re-reads MEMORY_ROOT from the env."""
    for mod in ("scheduler", "paths", "roster", "reminder_format"):
        sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------

def test_urgency_high_per_addressee() -> None:
    print("\n[1] urg:high — per-addressee fan-out + fast intervals")
    _setup_temp_memory(
        "## Family\n"
        "- **Ankit** — tg:1001\n"
        "- **Sunanda** — tg:1002, wa:+15555550002\n"
    )
    _reset_modules()
    import scheduler  # type: ignore

    sent: list[tuple[str, str]] = []
    with patch.object(
        scheduler.channels, "send_returning_msg_id",
        side_effect=lambda ident, body, **kw: (sent.append((ident, body)), 9999)[1],
    ):
        scheduler.start()
        # Reminder 5 minutes in the future, urg:high, two addressees.
        due = datetime.utcnow() + timedelta(minutes=5)
        line = (
            f"- [{due.strftime('%Y-%m-%d %H:%M')}] pickup baby "
            "@Ankit @Sunanda from:tg:1001 urg:high"
        )
        (scheduler.memories_dir() / "reminders.md").write_text(line + "\n")
        scheduler.reconcile()

        jobs = {j.id for j in scheduler.start().get_jobs()}
        # Expect: 2 fire jobs (one per addressee), 2 escalate jobs,
        # 1 fallback (since urg:high has fallback), 1 miss = 6 total.
        fire_jobs = {j for j in jobs if j.startswith("fire:")}
        esc_jobs = {j for j in jobs if j.startswith("escalate:")}
        fb_jobs = {j for j in jobs if j.startswith("fallback:")}
        miss_jobs = {j for j in jobs if j.startswith("miss:")}

        _check("two fire jobs (one per addressee)", len(fire_jobs) == 2,
               f"got {fire_jobs}")
        _check("two escalate jobs (one per addressee)", len(esc_jobs) == 2,
               f"got {esc_jobs}")
        _check("one fallback job", len(fb_jobs) == 1,
               f"got {fb_jobs}")
        _check("one miss job", len(miss_jobs) == 1,
               f"got {miss_jobs}")

        # Check escalate timing: high tier escalates at +3m from due.
        for j in scheduler.start().get_jobs():
            if j.id.startswith("escalate:"):
                expected = due + timedelta(minutes=3)
                # APScheduler stores tz-aware; due is naive (UTC). Compare
                # within a 60s window to absorb sub-minute drift.
                actual = j.next_run_time.replace(tzinfo=None)
                _check(
                    f"escalate fires at +3m ({j.id})",
                    abs((actual - expected).total_seconds()) < 60,
                    f"actual={actual} expected={expected}",
                )

        scheduler.shutdown(wait=False)


def test_urgency_low_short_ladder() -> None:
    print("\n[2] urg:low — fire + miss only, no escalate, no fallback")
    _setup_temp_memory(
        "## Family\n"
        "- **Ankit** — tg:1001\n"
    )
    _reset_modules()
    import scheduler  # type: ignore

    with patch.object(
        scheduler.channels, "send_returning_msg_id",
        side_effect=lambda *a, **kw: 1,
    ):
        scheduler.start()
        due = datetime.utcnow() + timedelta(minutes=5)
        line = (
            f"- [{due.strftime('%Y-%m-%d %H:%M')}] schedule annual physical "
            "@Ankit from:tg:1001 urg:low"
        )
        (scheduler.memories_dir() / "reminders.md").write_text(line + "\n")
        scheduler.reconcile()

        jobs = {j.id for j in scheduler.start().get_jobs()}
        _check("one fire job", sum(1 for j in jobs if j.startswith("fire:")) == 1)
        _check("zero escalate jobs",
               sum(1 for j in jobs if j.startswith("escalate:")) == 0)
        _check("zero fallback jobs",
               sum(1 for j in jobs if j.startswith("fallback:")) == 0)
        _check("one miss job (just for logging)",
               sum(1 for j in jobs if j.startswith("miss:")) == 1)

        scheduler.shutdown(wait=False)


def test_ack_cancels_chain() -> None:
    print("\n[3] ack on the line cancels every pending tier")
    _setup_temp_memory(
        "## Family\n"
        "- **Ankit** — tg:1001\n"
        "- **Sunanda** — tg:1002\n"
    )
    _reset_modules()
    import scheduler  # type: ignore

    sent: list[str] = []
    with patch.object(
        scheduler.channels, "send_returning_msg_id",
        side_effect=lambda ident, body, **kw: (sent.append(ident), 1)[1],
    ):
        scheduler.start()
        # Use a far-future timestamp so the jobs don't actually fire
        # mid-test. We're just exercising the self-skip pattern.
        due = datetime.utcnow() + timedelta(hours=1)
        line = (
            f"- [{due.strftime('%Y-%m-%d %H:%M')}] dinner res "
            "@Ankit @Sunanda from:tg:1001 urg:normal"
        )
        path = scheduler.memories_dir() / "reminders.md"
        path.write_text(line + "\n")
        scheduler.reconcile()

        # Confirm jobs were registered.
        jobs_before = {j.id for j in scheduler.start().get_jobs()}
        assert any(j.startswith("escalate:") for j in jobs_before)

        # Append an ack annotation directly.
        existing = path.read_text()
        # The line now has an id: appended by reconcile. Re-read it.
        line_with_id = existing.strip()
        annotated = line_with_id + " (acked by Sunanda at 2026-05-10 14:32)"
        path.write_text(annotated + "\n")

        # Manually invoke escalate_one for one addressee — it should
        # self-skip because of the ack annotation.
        ids = [
            tok.split(":", 1)[1]
            for tok in line_with_id.split()
            if tok.startswith("id:")
        ]
        task_id = ids[0]
        sent_before = len(sent)
        scheduler.escalate_one(
            task_id, "2026-05-10 14:00", line_with_id,
            "tg:1001", [("Ankit", "1001"), ("Sunanda", "1002")],
            addressee_name="Ankit", addressee_idents=["tg:1001"],
        )
        _check("escalate_one self-skips when (acked) is present",
               len(sent) == sent_before)

        # fallback_one should also self-skip.
        scheduler.fallback_one(
            task_id, "2026-05-10 14:00", line_with_id,
            "tg:1001", [("Ankit", "1001"), ("Sunanda", "1002")],
        )
        _check("fallback_one self-skips when (acked) is present",
               len(sent) == sent_before)

        # miss_one should also self-skip — line shouldn't move to ## Missed.
        scheduler.miss_one(
            task_id, "2026-05-10 14:00", line_with_id,
            "tg:1001", [("Ankit", "1001"), ("Sunanda", "1002")],
        )
        # Line should still be in head (or fired) — NOT in ## Missed.
        after = path.read_text()
        _check("miss_one self-skips when (acked) is present",
               "## Missed" not in after,
               f"file:\n{after}")

        scheduler.shutdown(wait=False)


def test_fallback_resolution_picks_non_addressee() -> None:
    print("\n[4] fallback resolves to a household member who isn't an addressee")
    _setup_temp_memory(
        "## Family\n"
        "- **Ankit** — tg:1001\n"
        "- **Sunanda** — tg:1002\n"
    )
    _reset_modules()
    import scheduler  # type: ignore

    fallback_name, fallback_idents = scheduler._resolve_fallback_recipient(
        "give Maya antibiotics @Ankit from:tg:1001 urg:high",
        "tg:1001",
    )
    _check(
        "addressee=Ankit → fallback=Sunanda",
        fallback_name.lower() == "sunanda" and fallback_idents == ["tg:1002"],
        f"got name={fallback_name!r} idents={fallback_idents}",
    )

    # Explicit fb: tag wins.
    fallback_name2, _ = scheduler._resolve_fallback_recipient(
        "thing @Ankit from:tg:1001 urg:high fb:Sunanda",
        "tg:1001",
    )
    _check("explicit fb:Sunanda resolves to Sunanda",
           fallback_name2.lower() == "sunanda")

    # Solo-household case: addressee is the only member → empty fallback.
    _setup_temp_memory("## Family\n- **Ankit** — tg:1001\n")
    _reset_modules()
    import scheduler as sched2  # type: ignore
    fb_name, fb_idents = sched2._resolve_fallback_recipient(
        "do laundry @Ankit from:tg:1001 urg:high",
        "tg:1001",
    )
    _check("solo household → no fallback",
           fb_name == "" and fb_idents == [],
           f"got name={fb_name!r} idents={fb_idents}")


def test_recent_fires_for_lookup() -> None:
    print("\n[5] recent_fires_for surfaces ack candidates for the agent")
    _setup_temp_memory(
        "## Family\n"
        "- **Ankit** — tg:1001\n"
    )
    _reset_modules()
    import scheduler  # type: ignore

    # Compose a reminders.md by hand with a fresh fire annotation.
    now = datetime.utcnow()
    fire_ts = now.strftime("%Y-%m-%d %H:%M")
    due_ts = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")
    line = (
        f"- [{due_ts}] pickup baby @Ankit from:tg:1001 urg:high id:abc123 "
        f"(fired at {fire_ts} chat:tg:1001 msg:9876)"
    )
    # Fired lines live under ## Fired.
    content = f"\n## Fired\n{line}\n"
    (scheduler.memories_dir() / "reminders.md").write_text(content)

    fires = scheduler.recent_fires_for("tg:1001", within_minutes=10)
    _check("one recent fire surfaced", len(fires) == 1, f"got {fires}")
    if fires:
        _check("task_id matches", fires[0]["task_id"] == "abc123",
               f"got {fires[0]}")
        _check("summary present and reasonable",
               "pickup" in fires[0]["summary"].lower(),
               f"got {fires[0]['summary']!r}")

    # An acked line should NOT surface.
    acked = line + " (acked by Ankit at 2026-05-10 14:00)"
    (scheduler.memories_dir() / "reminders.md").write_text(
        f"\n## Fired\n{acked}\n",
    )
    _check("acked line is excluded",
           scheduler.recent_fires_for("tg:1001", 10) == [])

    # Different identifier → no match.
    (scheduler.memories_dir() / "reminders.md").write_text(content)
    _check("identifier mismatch returns empty",
           scheduler.recent_fires_for("tg:9999", 10) == [])


def test_snooze_pattern() -> None:
    print("\n[6] snooze: ack old + new line both reconcile cleanly")
    _setup_temp_memory(
        "## Family\n"
        "- **Ankit** — tg:1001\n"
    )
    _reset_modules()
    import scheduler  # type: ignore

    with patch.object(
        scheduler.channels, "send_returning_msg_id",
        side_effect=lambda *a, **kw: 1,
    ):
        scheduler.start()
        original_due = datetime.utcnow() + timedelta(hours=1)
        snoozed_due = datetime.utcnow() + timedelta(hours=2)
        original_line = (
            f"- [{original_due.strftime('%Y-%m-%d %H:%M')}] dentist "
            "@Ankit from:tg:1001 urg:normal"
        )
        path = scheduler.memories_dir() / "reminders.md"
        path.write_text(original_line + "\n")
        scheduler.reconcile()

        # Now mimic snooze: ack the original, append a new line.
        existing = path.read_text().strip()
        snoozed_line = (
            f"- [{snoozed_due.strftime('%Y-%m-%d %H:%M')}] dentist "
            "@Ankit from:tg:1001 urg:normal"
        )
        new_content = (
            existing
            + " (acked by Ankit at 2026-05-10 14:00)"
            + "\n"
            + snoozed_line
            + "\n"
        )
        path.write_text(new_content)
        scheduler.reconcile()

        # The new line should have its own complete ladder, and the old
        # one's jobs should be orphaned (since its line got ack-annotated
        # and reconcile no longer schedules it). For a normal-tier solo
        # reminder: 1 fire + 1 escalate + 1 fallback + 1 miss = 4 jobs,
        # and the old chain should be gone.
        jobs = sorted(j.id for j in scheduler.start().get_jobs())
        # We expect EXACTLY 4 jobs total for the new (only un-acked) line.
        _check("snooze produces exactly 4 jobs (one ladder, normal tier)",
               len(jobs) == 4, f"got jobs={jobs}")

        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_urgency_high_per_addressee()
    test_urgency_low_short_ladder()
    test_ack_cancels_chain()
    test_fallback_resolution_picks_non_addressee()
    test_recent_fires_for_lookup()
    test_snooze_pattern()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL CHECKS PASSED")
