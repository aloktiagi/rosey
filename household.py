"""Render household.md into the memory directory from household.toml.

Run once at setup, or whenever the household roster changes. Usage:
    python -m household
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python <3.11
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

import roster
from paths import memories_dir


def _member_id(member: dict) -> str:
    """Return the per-member identifier in the canonical `tg:NNN` /
    `@username` form that the rest of rosey expects.

    Accepts three TOML shapes:
      * ``telegram_id = "100"`` — canonical, used by household.toml.example
        and v2 provisioning
      * ``telegram_username = "sarah_t"`` — v2 pending members who haven't
        first-messaged yet
      * ``phone = "tg:100"`` — legacy shape emitted by older versions of
        the router; kept for backwards compat
    """
    if tid := member.get("telegram_id"):
        # Strip an accidental "tg:" prefix if it leaked through.
        s = str(tid).strip()
        if s.startswith("tg:"):
            s = s[len("tg:"):]
        return f"tg:{s}"
    if username := member.get("telegram_username"):
        u = str(username).lstrip("@").lower()
        return f"@{u}" if u else ""
    if phone := member.get("phone"):
        s = str(phone).strip()
        # Already canonical?
        if s.startswith("tg:") or s.startswith("@"):
            return s
        return ""
    return ""


def render(config: dict) -> str:
    title = config.get("household_name") or "Household"
    members = config.get("members", [])
    lines = [f"# {title}", "", "Members:"]
    for m in members:
        ident = _member_id(m)
        notes = f" — {m['notes']}" if m.get("notes") else ""
        suffix = f" ({ident})" if ident else ""
        lines.append(f"- {m['name']}{suffix}{notes}")
    lines.append("")
    if cadence := config.get("shopping_cadence"):
        lines.append(f"Shopping cadence: {cadence}")
    if store := config.get("default_store"):
        lines.append(f"Default store: {store}")
    if backup := config.get("backup_store"):
        lines.append(f"Backup store: {backup}")
    if context := config.get("upfront_context"):
        lines.extend(["", "## About", "", context])
    lines.append("")
    return "\n".join(lines)


def members_from_household_md() -> list[dict]:
    """Backwards-compat wrapper around `roster.members()` returning dicts.

    New code should call `roster.members()` directly. This shim exists
    so existing fan-out callers don't break mid-refactor.
    """
    return [{"name": m.name, "id": m.identifier} for m in roster.members()]


def main() -> int:
    config_path = Path("household.toml")
    if not config_path.exists():
        print("household.toml not found. Copy household.toml.example and fill it in.", file=sys.stderr)
        return 1

    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    content = render(config)

    target_dir = memories_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "household.md"
    target.write_text(content, encoding="utf-8")
    print(f"wrote {target} ({len(content)} bytes, {len(config.get('members', []))} members)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
