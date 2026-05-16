"""Provision a per-household Fly app via flyctl.

Shells out to `flyctl` in subprocess. Honors ROUTER_DRY_RUN=1 to skip the
real Fly calls (default for tests + local dev). Set ROUTER_DRY_RUN=0 to
actually create apps.

Env vars consumed (passed to the household VM as secrets):
  ANTHROPIC_API_KEY, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN,
  ROSEY_INTERNAL_TOKEN — shared secret used by the router to forward later.

Plus router knobs:
  ROSEY_FLY_ORG (default "personal")
  ROSEY_FLY_REGION (default "sjc")
  ROSEY_SOURCE_IMAGE — registry.fly.io ref to clone household VMs from
    (default: latest from ROSEY_TEMPLATE_APP, default "rosey-template")
  ROSEY_TEMPLATE_APP (default "rosey-template")
  ROSEY_HOUSEHOLD_CONFIG — path to the fly.toml used as `--config` when
    deploying a new household VM (default: bundled household_template.fly.toml).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from . import db, notifications, telegram_onboarding

log = logging.getLogger(__name__)

DEFAULT_HOUSEHOLD_CONFIG = Path(__file__).resolve().parent / "household_template.fly.toml"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def kick_off(engine, admin_phone: str) -> None:
    """Spawn a background thread to provision a new household. Returns
    immediately so the inbound webhook handler doesn't block."""
    threading.Thread(target=_provision, args=(engine, admin_phone), daemon=True).start()


def _provision(engine, admin_phone: str) -> None:
    sess = db.get_onboarding(engine, admin_phone)
    if not sess:
        log.warning("provision called for %s but no onboarding session", admin_phone)
        return

    data = sess["data"]
    flow = data.get("flow", "v1")
    admin_name = data.get("admin_name") or "Admin"
    members = data.get("members", [])
    fly_app_name = f"rosey-h-{secrets.token_hex(4)}"

    dry_run = os.environ.get("ROUTER_DRY_RUN", "1") == "1"
    log.info(
        "provisioning %s flow=%s (%s + %d members, dry_run=%s)",
        fly_app_name, flow, admin_name, len(members), dry_run,
    )

    try:
        if dry_run:
            time.sleep(1)  # simulate
        else:
            _provision_real(
                fly_app_name,
                admin_name,
                admin_phone,
                members,
                household_name=data.get("household_name"),
                timezone=data.get("timezone"),
                upfront_context=data.get("upfront_context"),
            )
    except Exception:
        log.exception("provisioning failed for %s", fly_app_name)
        # Leave session in PROVISIONING state so they don't accidentally
        # restart onboarding; admin handles via DB or restart.
        return

    # Commit DB rows. The shape diverges by flow:
    #   v1: members in the data blob are already-known {name, phone}.
    #       Insert each as an active member.
    #   v2: members are pre-roster {name, tg_username}. Insert as pending
    #       placeholders; the admin will share invite codes with them.
    household_id = db.create_household(engine, fly_app_name, status="active")
    db.add_member(
        engine, admin_phone, household_id, admin_name,
        email=data.get("email"),
    )

    invite_codes: list[dict] = []
    if flow == "v2":
        for m in members:
            placeholder = db.add_pending_member(
                engine,
                household_id,
                m["name"],
                tg_username=m.get("tg_username"),
            )
            code = telegram_onboarding.generate_invite_code(
                engine,
                member={"household_id": household_id, "phone": placeholder},
                invitee_name=m["name"],
            )
            invite_codes.append({"name": m["name"], "code": code})
    else:
        for m in members:
            db.add_member(engine, m["phone"], household_id, m["name"])

    db.delete_onboarding(engine, admin_phone)

    log.info("provisioning %s active id=%s flow=%s", fly_app_name, household_id, flow)

    if flow == "v2":
        _send_v2_welcome(
            admin_phone=admin_phone,
            admin_name=admin_name,
            household_name=data.get("household_name") or "your household",
            invite_codes=invite_codes,
        )
    else:
        _send_welcomes(admin_name, admin_phone, members)


# ---------------------------------------------------------------------------
# Real provisioning via flyctl
# ---------------------------------------------------------------------------

def _provision_real(
    app_name: str,
    admin_name: str,
    admin_phone: str,
    members: list,
    household_name: Optional[str] = None,
    timezone: Optional[str] = None,
    upfront_context: Optional[str] = None,
) -> None:
    org = os.environ.get("ROSEY_FLY_ORG", "personal")
    region = os.environ.get("ROSEY_FLY_REGION", "sjc")
    config_path = os.environ.get("ROSEY_HOUSEHOLD_CONFIG", str(DEFAULT_HOUSEHOLD_CONFIG))
    source_image = os.environ.get("ROSEY_SOURCE_IMAGE") or _latest_image_of(
        os.environ.get("ROSEY_TEMPLATE_APP", "rosey-template")
    )

    if not Path(config_path).exists():
        raise RuntimeError(f"household config not found: {config_path}")

    secrets_kv = _collect_secrets(
        admin_name,
        admin_phone,
        members,
        household_name=household_name,
        timezone=timezone,
        upfront_context=upfront_context,
    )

    # 1. Create the app
    _run_fly("apps", "create", app_name, "--org", org)

    # 2. Create the persistent volume for memory state
    _run_fly(
        "volumes", "create", "memory_data",
        "--size", "1", "--region", region,
        "-a", app_name, "--yes",
    )

    # 3. Stage secrets (no deploy yet — VM doesn't exist)
    secret_args = [f"{k}={v}" for k, v in secrets_kv.items()]
    _run_fly("secrets", "set", *secret_args, "--stage", "-a", app_name)

    # 4. Deploy from the template image. --image skips build, --config gives
    #    runtime layout (mounts, http_service). cwd doesn't matter.
    _run_fly(
        "deploy",
        "--image", source_image,
        "-a", app_name,
        "--config", config_path,
        "--yes",
        timeout=600,
    )


def _collect_secrets(
    admin_name: str,
    admin_phone: str,
    members: list,
    household_name: Optional[str] = None,
    timezone: Optional[str] = None,
    upfront_context: Optional[str] = None,
) -> dict:
    required = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ROSEY_INTERNAL_TOKEN",
        "TELEGRAM_BOT_TOKEN",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"missing env vars for provisioning: {missing}")

    secrets_kv = {k: os.environ[k] for k in required}
    secrets_kv["HOUSEHOLD_TOML"] = _render_household_toml(
        admin_name,
        admin_phone,
        members,
        household_name=household_name,
        upfront_context=upfront_context,
    )
    # Per-household timezone overrides the global default in household_template.fly.toml.
    # When None we leave the env default in place (currently America/Los_Angeles).
    if timezone:
        secrets_kv["SCHEDULER_TZ"] = timezone
    return secrets_kv


def _render_household_toml(
    admin_name: str,
    admin_phone: str,
    members: list,
    household_name: Optional[str] = None,
    upfront_context: Optional[str] = None,
) -> str:
    """Render the household.toml the VM consumes via HOUSEHOLD_TOML env var.

    Emits the canonical field names that ``household.py`` understands:
    ``telegram_id`` (numeric, no "tg:" prefix) for known members, and
    ``telegram_username`` for v2 pre-rostered placeholders.
    """
    blocks: list[str] = []
    if household_name:
        blocks.append(f"household_name = {_toml_str(household_name)}")
    blocks.append('shopping_cadence = "weekly"')
    if upfront_context:
        blocks.append(f"upfront_context = {_toml_str(upfront_context)}")
    blocks.extend([
        "",
        "[[members]]",
        f"name = {_toml_str(admin_name)}",
    ])
    blocks.extend(_id_lines(admin_phone))
    blocks.append('notes = ""')
    for m in members:
        blocks.extend(["", "[[members]]", f"name = {_toml_str(m['name'])}"])
        if m.get("phone"):
            # v1 shape: "tg:NNN" string in m["phone"]
            blocks.extend(_id_lines(m["phone"]))
        elif m.get("tg_username"):
            # v2 pending member with a known username
            blocks.append(
                f"telegram_username = {_toml_str(m['tg_username'].lstrip('@').lower())}"
            )
        blocks.append('notes = ""')
    return "\n".join(blocks) + "\n"


def _id_lines(identifier: str) -> list:
    """Convert an internal "tg:NNN" identifier to TOML ``telegram_id``
    lines. Returns [] for missing/unparseable identifiers so the caller
    can still emit the rest of the member block."""
    if not identifier:
        return []
    s = identifier.strip()
    if s.startswith("tg:"):
        return [f"telegram_id = {_toml_str(s[len('tg:'):])}"]
    if s.startswith("@"):
        return [f"telegram_username = {_toml_str(s.lstrip('@').lower())}"]
    # Unrecognized — keep the original under `phone` for backwards compat
    return [f"phone = {_toml_str(s)}"]


def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _latest_image_of(app: str) -> str:
    out = _run_fly("image", "show", "-a", app, "--json")
    data = json.loads(out)
    if isinstance(data, list):
        data = data[0]
    return f"{data['Registry']}/{data['Repository']}:{data['Tag']}"


def _run_fly(*args: str, cwd: Optional[str] = None, timeout: int = 300) -> str:
    cmd = ["fly", *args]
    # Don't echo full secrets; just the verb + flags before the first key=value
    safe_preview = " ".join(a if "=" not in a else a.split("=", 1)[0] + "=***" for a in cmd)
    log.info("$ %s", safe_preview)
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        # Trim stderr; flyctl is verbose
        msg = result.stderr.strip()[-500:] or result.stdout.strip()[-500:]
        raise RuntimeError(f"flyctl {args[0]} failed (exit {result.returncode}): {msg}")
    return result.stdout


# ---------------------------------------------------------------------------
# Welcome message via Telegram
# ---------------------------------------------------------------------------

def _send_welcomes(admin_name: str, admin_id: str, members: list) -> None:
    """v1 welcome: simple DM to admin and each pre-listed member."""
    others_for_admin = [{"name": m["name"], "id": m["phone"]} for m in members]
    _send_welcome(admin_id, admin_name, others_for_admin)
    for m in members:
        others = [{"name": admin_name, "id": admin_id}] + [
            {"name": x["name"], "id": x["phone"]} for x in members if x["phone"] != m["phone"]
        ]
        _send_welcome(m["phone"], m["name"], others)


def _welcome_body(name: str, others: list) -> str:
    if not others:
        return (
            f"✅ Hi {name}! You're all set up with Rosey.\n\n"
            "Try 'add bananas' or 'remember the wifi password is goldfinch42'.\n\n"
            "To add family members, send: /invite <their name>"
        )
    names = ", ".join(o["name"] for o in others)
    return (
        f"✅ Hi {name}! You and {names} are now connected to Rosey. "
        "Try 'add bananas' or 'find a plumber near us'."
    )


def _send_welcome(identifier: str, name: str, others: list) -> None:
    """v1 welcome DM. Skips non-Telegram identifiers."""
    if not identifier.startswith("tg:"):
        log.warning("welcome skipped — non-Telegram identifier %s", identifier)
        return
    chat_id = int(identifier[len("tg:"):])
    if notifications.send_text(chat_id, _welcome_body(name, others)):
        log.info("welcome sent to tg:%s", chat_id)


def _send_v2_welcome(
    admin_phone: str,
    admin_name: str,
    household_name: str,
    invite_codes: list,
) -> None:
    """v2 welcome to the admin: confirms setup, lists invite codes for any
    pre-rostered family, and offers a deep-link button to create the
    family group.

    ``invite_codes`` is a list of ``{name, code}`` dicts. If empty, the
    welcome just nudges them toward the group button.
    """
    if not admin_phone.startswith("tg:"):
        log.warning("v2 welcome skipped — non-Telegram identifier %s", admin_phone)
        return
    chat_id = int(admin_phone[len("tg:"):])

    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    button_url = (
        f"https://t.me/{bot_username}?startgroup=ready"
        if bot_username
        else "https://t.me/RoseyHouseholdBot?startgroup=ready"
    )

    lines = [f"🎉 **{household_name}** is set up, {admin_name}!", ""]
    if invite_codes:
        lines.append("Codes to share with family (each works once, expires in 7 days):")
        lines.append("")
        for entry in invite_codes:
            lines.append(f"• **{entry['name']}** → `{entry['code']}`")
        lines.append("")
        lines.append(
            "They each open Rosey on Telegram and paste their code."
        )
        lines.append("")

    lines.append(
        "Next: tap the button below to create your family group. "
        "Rosey will join, and anyone in the group can text me to add to the list, "
        "set reminders, or ask what's on the calendar."
    )
    body = "\n".join(lines)

    ok = notifications.send_with_url_button(
        chat_id,
        body,
        button_label="Create family group",
        button_url=button_url,
        parse_mode="Markdown",
    )
    if ok:
        log.info("v2 welcome sent to tg:%s codes=%d", chat_id, len(invite_codes))
    else:
        log.warning("v2 welcome failed for tg:%s", chat_id)
