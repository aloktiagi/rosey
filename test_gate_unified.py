"""Tests for the unified group-chat gate.

Covers:
  1. `explicit_name_trigger` — "rosey ..." / "hey rosey ..." / bare
     "rosey" / casing variants / negative cases like "rosemary".
  2. `classify_group_message` — explicit triggers bypass the LLM
     classifier entirely; non-triggered text falls through to the
     fuzzy classifier (mocked here so the test doesn't burn an API
     call); strict mode returns False without calling the classifier.

Run with: PYTHONPATH=. python3 test_gate_unified.py
"""
from __future__ import annotations

import os
import sys
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
# explicit_name_trigger
# ---------------------------------------------------------------------------
def test_explicit_name_trigger() -> None:
    from gate import explicit_name_trigger

    cases: list[tuple[str, bool, str]] = [
        # (input, expected_matched, expected_cleaned)
        ("rosey, what's the wifi",      True,  "what's the wifi"),
        ("Rosey: drink water",          True,  "drink water"),
        ("rosey what's up",             True,  "what's up"),
        ("hey rosey, remind me",        True,  "remind me"),
        ("Hey Rosey: pick up Maya",     True,  "pick up Maya"),
        ("rosey",                       True,  ""),                # bare prefix
        ("HEY ROSEY",                   True,  ""),                # case-insensitive bare
        # Negatives
        ("rosemary doesn't count",      False, "rosemary doesn't count"),
        ("the babysitter texted Rosey", False, "the babysitter texted Rosey"),
        ("we need milk",                False, "we need milk"),
        ("",                            False, ""),
    ]
    for text, expect_matched, expect_cleaned in cases:
        matched, cleaned = explicit_name_trigger(text)
        ok = (matched == expect_matched) and (cleaned == expect_cleaned)
        check(
            f"trigger: {text!r:50} → ({expect_matched}, {expect_cleaned!r})",
            ok,
            detail=f"got ({matched}, {cleaned!r})",
        )


# ---------------------------------------------------------------------------
# classify_group_message — explicit name prefix bypasses the LLM
# ---------------------------------------------------------------------------
def test_classify_explicit_skips_llm() -> None:
    import gate

    # `should_respond_in_group` should NOT be called when the name
    # prefix matches — bypassing the LLM keeps cost and latency at zero
    # for the most common case.
    with patch("gate.should_respond_in_group") as mock_classifier:
        respond, cleaned = gate.classify_group_message("rosey, what's the wifi")
        check(
            "classify: explicit trigger → True without LLM call",
            respond is True and cleaned == "what's the wifi" and mock_classifier.call_count == 0,
            detail=f"got respond={respond} cleaned={cleaned!r} llm_calls={mock_classifier.call_count}",
        )


# ---------------------------------------------------------------------------
# classify_group_message — non-trigger text falls through to fuzzy classifier
# ---------------------------------------------------------------------------
def test_classify_falls_through_to_fuzzy() -> None:
    import gate

    # Force fuzzy ON.
    with patch.dict(os.environ, {"ROSEY_FUZZY_TRIGGER": "on"}, clear=False), \
         patch("gate.should_respond_in_group", return_value=True) as mock_yes:
        respond, cleaned = gate.classify_group_message("we need milk")
        check(
            "classify: no prefix, fuzzy YES → True (text unchanged)",
            respond is True and cleaned == "we need milk" and mock_yes.call_count == 1,
            detail=f"respond={respond} cleaned={cleaned!r}",
        )

    with patch.dict(os.environ, {"ROSEY_FUZZY_TRIGGER": "on"}, clear=False), \
         patch("gate.should_respond_in_group", return_value=False) as mock_no:
        respond, _ = gate.classify_group_message("haha lol")
        check(
            "classify: no prefix, fuzzy NO → False",
            respond is False and mock_no.call_count == 1,
            detail=f"respond={respond}",
        )


# ---------------------------------------------------------------------------
# classify_group_message — strict mode (fuzzy OFF) skips the LLM
# ---------------------------------------------------------------------------
def test_classify_strict_mode_no_llm() -> None:
    import gate

    with patch.dict(os.environ, {"ROSEY_FUZZY_TRIGGER": "off"}, clear=False), \
         patch("gate.should_respond_in_group") as mock_classifier:
        respond, cleaned = gate.classify_group_message("we need milk")
        check(
            "classify: strict mode, no prefix → False without LLM call",
            respond is False and mock_classifier.call_count == 0,
            detail=f"respond={respond} llm_calls={mock_classifier.call_count}",
        )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))

    print("─" * 60)
    print(" Unified group-chat gate")
    print("─" * 60)

    test_explicit_name_trigger()
    test_classify_explicit_skips_llm()
    test_classify_falls_through_to_fuzzy()
    test_classify_strict_mode_no_llm()

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
