"""Tests for the four reminder-formatting fixes shipped on 2026-05-13.

Covers:
  1. FROM_RE matches tg, wa:+, wa:group:, and alexa: identifier shapes.
  2. _strip_to_user_message removes urg: and fb: in addition to the
     previously-stripped tags. Verifies metadata-free output for a
     realistic group-routed reminder line.
  3. send_whatsapp (Cloud API path) strips Telegram HTML before sending.
     Stubs the actual HTTP call.
  4. scan_pending_ack_broadcasts sends a single completion notice to a
     group origin per acked line, then no-ops on a second pass.

Run with: PYTHONPATH=. python3 test_reminder_fixes.py
"""

from __future__ import annotations

import os
import sys
import tempfile
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
# 1. FROM_RE handles all channel identifier shapes
# ---------------------------------------------------------------------------
def test_from_re_shapes() -> None:
    from reminder_format import FROM_RE

    cases: list[tuple[str, str]] = [
        ("from:tg:8600355980 urg:normal", "tg:8600355980"),
        ("from:tg:-100123456 urg:high", "tg:-100123456"),
        ("from:wa:+15048755536 ", "wa:+15048755536"),
        ("from:wa:group:120363408351089900.us ", "wa:group:120363408351089900.us"),
        ("from:wa:group:120363@g.us miss:1h", "wa:group:120363@g.us"),
        ("from:alexa:amzn1.ask.account.AAAA esc:5m", "alexa:amzn1.ask.account.AAAA"),
    ]
    for text, expected in cases:
        m = FROM_RE.search(text)
        captured = m.group(1) if m else None
        check(
            f"FROM_RE: {text.strip()} → {expected}",
            captured == expected,
            detail=f"got {captured!r}",
        )


# ---------------------------------------------------------------------------
# 2. _strip_to_user_message removes urg: / fb: + everything from before
# ---------------------------------------------------------------------------
def test_strip_to_user_message() -> None:
    from scheduler import _strip_to_user_message

    cases: list[tuple[str, str]] = [
        # The actual line from the screenshot bug — should produce just
        # "drink water 💧" with no metadata residue.
        (
            "drink water 💧 @Ankit from:wa:group:120363408351089900.us urg:normal id:abc123",
            "drink water 💧",
        ),
        # urg: was the one that leaked before — confirm it's gone.
        ("call vet urg:high", "call vet"),
        # fb: shouldn't show up in the user-facing body either.
        ("pick up Maya fb:Sunanda", "pick up Maya"),
        # All of the above plus an (annotation), to confirm parens still strip.
        (
            "water plants urg:low miss:1d (fired at 2026-05-13 10:00 chat:tg:123 msg:456)",
            "water plants",
        ),
        # Plain message (no tags) — passes through unchanged except for whitespace.
        ("buy bread", "buy bread"),
    ]
    for raw, expected in cases:
        got = _strip_to_user_message(raw)
        check(
            f"strip: {raw[:48]}{'…' if len(raw) > 48 else ''} → {expected!r}",
            got == expected,
            detail=f"got {got!r}",
        )


# ---------------------------------------------------------------------------
# 3. send_whatsapp strips HTML before dispatch (both transports)
# ---------------------------------------------------------------------------
def test_whatsapp_html_strip() -> None:
    import channels

    # Stub the env so _send_via_cloud_api gets credentials and proceeds.
    with (
        patch.dict(
            os.environ,
            {
                "WHATSAPP_ACCESS_TOKEN": "test-token",
                "WHATSAPP_PHONE_NUMBER_ID": "123",
                "BAILEYS_MODE": "off",
            },
            clear=False,
        ),
        patch("channels.urllib.request.urlopen") as urlopen,
    ):
        # urlopen needs to behave as a context manager returning .read() + .status.
        class _Resp:
            status = 200

            def read(self):
                return b'{"messages":[{"id":"wamid.XXX"}]}'

        urlopen.return_value.__enter__.return_value = _Resp()

        html_body = '⏰ Reminder for <a href="tg://user?id=8600355980">Ankit</a>: drink water 💧'
        result = channels.send_whatsapp("15551234567", html_body)
        # The body that landed in the POST should be HTML-free.
        sent_payload = urlopen.call_args[0][0].data.decode("utf-8")
        check(
            "whatsapp Cloud API HTML strip: no <a> tag in outgoing body",
            "<a href" not in sent_payload,
            detail=sent_payload[:200],
        )
        check(
            "whatsapp Cloud API HTML strip: name still present after strip",
            "Ankit" in sent_payload,
            detail=sent_payload[:200],
        )
        check(
            "whatsapp Cloud API HTML strip: returned wamid",
            result == "wamid.XXX",
            detail=str(result),
        )

    # Baileys path: same input, same expectation.
    with (
        patch.dict(
            os.environ,
            {
                "BAILEYS_MODE": "on",
                "BAILEYS_BRIDGE_SECRET": "test-bridge",
            },
            clear=False,
        ),
        patch("channels.urllib.request.urlopen") as urlopen,
    ):

        class _Resp:
            def read(self):
                return b'{"id":"baileys-msg-XXX"}'

        urlopen.return_value.__enter__.return_value = _Resp()
        result = channels.send_whatsapp(
            "group:120363408351089900@g.us",
            '⏰ Reminder for <a href="tg://user?id=8600355980">Ankit</a>: drink water 💧',
        )
        sent_payload = urlopen.call_args[0][0].data.decode("utf-8")
        check(
            "whatsapp Baileys HTML strip: no <a> tag in outgoing body",
            "<a href" not in sent_payload,
            detail=sent_payload[:200],
        )
        check(
            "whatsapp Baileys HTML strip: name still present after strip",
            "Ankit" in sent_payload,
            detail=sent_payload[:200],
        )


# ---------------------------------------------------------------------------
# 4. scan_pending_ack_broadcasts — sends to group origin, idempotent
# ---------------------------------------------------------------------------
def test_ack_broadcast() -> None:
    import scheduler

    with tempfile.TemporaryDirectory() as tmp:
        mem = Path(tmp) / "memories"
        mem.mkdir()
        reminders = mem / "reminders.md"
        # Two reminders: one with a group from: (should broadcast), one
        # with a 1:1 tg from: (should NOT broadcast).
        reminders.write_text(
            "# Reminders\n\n"
            "## Fired\n"
            "- [2026-05-13 17:51] drink water 💧 @Ankit "
            "from:wa:group:120363408351089900.us urg:normal id:abc123def456 "
            "(fired at 2026-05-13 17:51 chat:wa:+15048755536 msg:1) "
            "(acked by Ankit at 2026-05-13 19:37)\n"
            "- [2026-05-13 18:00] call mom @Ankit "
            "from:tg:8600355980 urg:normal id:fed654cba321 "
            "(fired at 2026-05-13 18:00 chat:tg:8600355980 msg:2) "
            "(acked by Ankit at 2026-05-13 19:38)\n"
        )

        with (
            patch("scheduler.memories_dir", return_value=mem),
            patch("channels.send", return_value=True) as send_mock,
        ):
            n = scheduler.scan_pending_ack_broadcasts()
            check(
                "ack broadcast: exactly 1 broadcast sent on first pass", n == 1, detail=f"got {n}"
            )
            # Verify the call went to the group origin.
            sent_calls = send_mock.call_args_list
            check(
                "ack broadcast: target was the group origin",
                any("wa:group:" in str(c.args[0]) for c in sent_calls),
                detail=str(sent_calls),
            )
            # Verify the broadcast body has the completion phrasing
            # AND has been stripped of metadata.
            body = sent_calls[0].args[1]
            check(
                "ack broadcast: body has ✓ + name + message",
                "✓ Ankit completed: drink water 💧" == body,
                detail=repr(body),
            )

            # Second pass should be a noop — annotation prevents re-send.
            n2 = scheduler.scan_pending_ack_broadcasts()
            check(
                "ack broadcast: idempotent on second pass (0 new sends)",
                n2 == 0,
                detail=f"got {n2}",
            )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Make sure we import from the repo root, not the venv site-packages.
    sys.path.insert(0, str(Path(__file__).parent))

    print("─" * 60)
    print(" Reminder formatting + ack-broadcast fixes")
    print("─" * 60)

    test_from_re_shapes()
    test_strip_to_user_message()
    test_whatsapp_html_strip()
    test_ack_broadcast()

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
