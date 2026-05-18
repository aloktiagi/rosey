"""Tests for recurring reminders (the `repeat:` tag).

Covers:
  1. REPEAT_RE matches the supported interval forms.
  2. _parse_repeat_interval returns correct timedeltas.
  3. _strip_to_user_message removes the repeat: tag from user-facing text.
  4. _maybe_schedule_next_occurrence writes the next line correctly
     after a daily reminder fires.
  5. _maybe_schedule_next_occurrence is idempotent — calling it twice
     with the same input produces the same single new line, not two.
  6. A non-repeating reminder is a no-op (no next line written).
  7. The new line carries the same body and `repeat:` tag so the chain
     continues; lifecycle annotations from the previous line are stripped.

Run with: PYTHONPATH=. python3 test_recurring_reminders.py
"""

from __future__ import annotations

import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((condition, name))
    marker = PASS if condition else FAIL
    extra = f"  ({detail})" if detail and not condition else ""
    print(f"{marker} {name}{extra}")


# ---------------------------------------------------------------------------
# 1. REPEAT_RE shapes
# ---------------------------------------------------------------------------
def test_repeat_re_shapes() -> None:
    from reminder_format import REPEAT_RE

    cases: list[tuple[str, str | None]] = [
        ("repeat:daily", "daily"),
        ("repeat:weekly", "weekly"),
        ("repeat:hourly", "hourly"),
        ("repeat:5m", "5m"),
        ("repeat:2h", "2h"),
        ("repeat:3d", "3d"),
        ("repeat:90s", None),  # seconds not supported
        ("repeat:monthly", None),  # only daily/weekly/hourly/Nm/Nh/Nd
        ("nrepeat:daily", None),  # word-boundary
        ("", None),
    ]
    for text, expected in cases:
        m = REPEAT_RE.search(text)
        got = m.group(1) if m else None
        check(
            f"REPEAT_RE: {text!r:25} → {expected!r}",
            got == expected,
            detail=f"got {got!r}",
        )


# ---------------------------------------------------------------------------
# 2. _parse_repeat_interval returns correct timedeltas
# ---------------------------------------------------------------------------
def test_parse_interval() -> None:
    from scheduler import _parse_repeat_interval

    cases: list[tuple[str, timedelta | None]] = [
        ("daily", timedelta(days=1)),
        ("weekly", timedelta(days=7)),
        ("hourly", timedelta(hours=1)),
        ("5m", timedelta(minutes=5)),
        ("2h", timedelta(hours=2)),
        ("3d", timedelta(days=3)),
        ("", None),
        ("bogus", None),
    ]
    for spec, expected in cases:
        got = _parse_repeat_interval(spec)
        check(
            f"parse_interval: {spec!r:15} → {expected!r}",
            got == expected,
            detail=f"got {got!r}",
        )


# ---------------------------------------------------------------------------
# 3. _strip_to_user_message removes repeat:
# ---------------------------------------------------------------------------
def test_strip_repeat() -> None:
    from scheduler import _strip_to_user_message

    cases: list[tuple[str, str]] = [
        (
            "give Siya her vitamin D drops 💧 urg:normal repeat:daily",
            "give Siya her vitamin D drops 💧",
        ),
        ("wash hands @Mamta repeat:daily from:wa:group:123.us", "wash hands"),
        # Existing strip behaviors still hold:
        ("call vet urg:high (acked by Ankit at 2026-05-13 10:00)", "call vet"),
    ]
    for raw, expected in cases:
        got = _strip_to_user_message(raw)
        check(
            f"strip: {raw[:48]}{'…' if len(raw) > 48 else ''} → {expected!r}",
            got == expected,
            detail=f"got {got!r}",
        )


# ---------------------------------------------------------------------------
# 4. _maybe_schedule_next_occurrence writes a next line for daily
# ---------------------------------------------------------------------------
def test_schedules_next_line_daily() -> None:
    import scheduler

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memories"
        mem.mkdir()
        reminders = mem / "reminders.md"
        # Fire happened — current line is in Fired section, with daily repeat.
        reminders.write_text(
            "# Reminders\n\n"
            "## Fired\n"
            "- [2026-05-13 09:00] give Siya her vitamin D drops 💧 @Ankit @Sunanda "
            "from:wa:group:120363.us urg:normal repeat:daily id:aaabbbccc111 "
            "(fired at 2026-05-13 09:00 chat:wa:group:120363.us msg:1)\n"
        )

        with (
            patch("scheduler.memories_dir", return_value=mem),
            patch("scheduler.reconcile") as mock_reconcile,
        ):
            wrote = scheduler._maybe_schedule_next_occurrence(
                "aaabbbccc111",
                "2026-05-13 09:00",
                "give Siya her vitamin D drops 💧 @Ankit @Sunanda "
                "from:wa:group:120363.us urg:normal repeat:daily id:aaabbbccc111 "
                "(fired at 2026-05-13 09:00 chat:wa:group:120363.us msg:1)",
            )
            check("recur: returned True for daily reminder", wrote is True)
            check(
                "recur: reconcile invoked",
                mock_reconcile.call_count == 1,
                detail=f"calls={mock_reconcile.call_count}",
            )

            new_content = reminders.read_text(encoding="utf-8")
            # Next line should be in the head section at 2026-05-14 09:00.
            check(
                "recur: head section has next-day line",
                "[2026-05-14 09:00]" in new_content,
                detail=new_content,
            )
            # Body should preserve the message and repeat tag.
            check(
                "recur: next line carries the repeat tag",
                "repeat:daily" in new_content.split("## Fired")[0],
                detail=new_content.split("## Fired")[0],
            )
            check(
                "recur: next line carries the message body",
                "give Siya her vitamin D drops" in new_content.split("## Fired")[0],
                detail=new_content.split("## Fired")[0],
            )
            # Lifecycle annotations should NOT be carried over.
            head_text = new_content.split("## Fired")[0]
            check(
                "recur: lifecycle annotations stripped from new line",
                "(fired at" not in head_text,
                detail=head_text,
            )


# ---------------------------------------------------------------------------
# 5. Idempotency — calling twice writes the same line, not two
# ---------------------------------------------------------------------------
def test_idempotent() -> None:
    import scheduler

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memories"
        mem.mkdir()
        reminders = mem / "reminders.md"
        reminders.write_text(
            "# Reminders\n\n"
            "## Fired\n"
            "- [2026-05-13 09:00] vitamin D drops @Ankit "
            "from:wa:group:120363.us urg:normal repeat:daily id:aaabbbccc111 "
            "(fired at 2026-05-13 09:00 chat:wa:group:120363.us msg:1)\n"
        )

        with patch("scheduler.memories_dir", return_value=mem), patch("scheduler.reconcile"):
            scheduler._maybe_schedule_next_occurrence(
                "aaabbbccc111",
                "2026-05-13 09:00",
                "vitamin D drops @Ankit from:wa:group:120363.us urg:normal "
                "repeat:daily id:aaabbbccc111",
            )
            content_after_first = reminders.read_text(encoding="utf-8")
            # Second call with identical input → no-op.
            scheduler._maybe_schedule_next_occurrence(
                "aaabbbccc111",
                "2026-05-13 09:00",
                "vitamin D drops @Ankit from:wa:group:120363.us urg:normal "
                "repeat:daily id:aaabbbccc111",
            )
            content_after_second = reminders.read_text(encoding="utf-8")
            check(
                "recur: idempotent (file unchanged on second call)",
                content_after_first == content_after_second,
                detail="files diverged",
            )
            # Exactly ONE new line in head.
            head = content_after_second.split("## Fired")[0]
            next_count = head.count("[2026-05-14")
            check(
                "recur: exactly one next-day line in head, not two",
                next_count == 1,
                detail=f"count={next_count}",
            )


# ---------------------------------------------------------------------------
# 6. Non-repeating reminder is a no-op
# ---------------------------------------------------------------------------
def test_non_repeating_is_noop() -> None:
    import scheduler

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memories"
        mem.mkdir()
        reminders = mem / "reminders.md"
        original = (
            "# Reminders\n\n"
            "## Fired\n"
            "- [2026-05-13 09:00] one-off task @Ankit "
            "from:wa:group:120363.us urg:normal id:aaabbbccc111\n"
        )
        reminders.write_text(original)

        with (
            patch("scheduler.memories_dir", return_value=mem),
            patch("scheduler.reconcile") as mock_reconcile,
        ):
            wrote = scheduler._maybe_schedule_next_occurrence(
                "aaabbbccc111",
                "2026-05-13 09:00",
                "one-off task @Ankit from:wa:group:120363.us urg:normal id:aaabbbccc111",
            )
            check("recur: no repeat tag → returns False", wrote is False)
            check("recur: reconcile NOT called for non-repeating", mock_reconcile.call_count == 0)
            check("recur: file unchanged", reminders.read_text(encoding="utf-8") == original)


# ---------------------------------------------------------------------------
# 7. Hourly repeat (sanity for non-daily intervals)
# ---------------------------------------------------------------------------
def test_hourly_interval() -> None:
    import scheduler

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memories"
        mem.mkdir()
        reminders = mem / "reminders.md"
        reminders.write_text(
            "# Reminders\n\n"
            "## Fired\n"
            "- [2026-05-13 09:00] check monitor @Ankit "
            "from:wa:group:120363.us urg:low repeat:2h id:bbbcccddd222 "
            "(fired at 2026-05-13 09:00 chat:wa:group:120363.us msg:1)\n"
        )

        with patch("scheduler.memories_dir", return_value=mem), patch("scheduler.reconcile"):
            scheduler._maybe_schedule_next_occurrence(
                "bbbcccddd222",
                "2026-05-13 09:00",
                "check monitor @Ankit from:wa:group:120363.us urg:low repeat:2h id:bbbcccddd222",
            )
            content = reminders.read_text(encoding="utf-8")
            check(
                "recur: 2-hour repeat schedules at +2h",
                "[2026-05-13 11:00]" in content,
                detail=content,
            )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))

    print("─" * 60)
    print(" Recurring reminders (repeat: tag)")
    print("─" * 60)

    test_repeat_re_shapes()
    test_parse_interval()
    test_strip_repeat()
    test_schedules_next_line_daily()
    test_idempotent()
    test_non_repeating_is_noop()
    test_hourly_interval()

    print("─" * 60)
    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    if passed == total:
        print(f"{PASS} {passed}/{total} checks passed")
        sys.exit(0)
    else:
        failed = [name for ok, name in results if not ok]
        print(f"{FAIL} {passed}/{total} checks passed — {total - passed} failed:")
        for name in failed:
            print(f"   - {name}")
        sys.exit(1)
