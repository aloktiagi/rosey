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

from . import db

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
    admin_name = data["admin_name"]
    members = data.get("members", [])
    fly_app_name = f"rosey-h-{secrets.token_hex(4)}"

    dry_run = os.environ.get("ROUTER_DRY_RUN", "1") == "1"
    log.info(
        "provisioning %s (%s + %d members, dry_run=%s)",
        fly_app_name, admin_name, len(members), dry_run,
    )

    try:
        if dry_run:
            time.sleep(1)  # simulate
        else:
            _provision_real(fly_app_name, admin_name, admin_phone, members)
    except Exception:
        log.exception("provisioning failed for %s", fly_app_name)
        # Leave session in PROVISIONING state so they don't accidentally
        # restart onboarding; admin handles via DB or restart.
        return

    # Commit DB rows
    household_id = db.create_household(engine, fly_app_name, status="active")
    db.add_member(engine, admin_phone, household_id, admin_name)
    for m in members:
        db.add_member(engine, m["phone"], household_id, m["name"])
    db.delete_onboarding(engine, admin_phone)

    log.info("provisioning %s active id=%s", fly_app_name, household_id)
    _send_welcomes(admin_name, admin_phone, members)


# ---------------------------------------------------------------------------
# Real provisioning via flyctl
# ---------------------------------------------------------------------------

def _provision_real(app_name: str, admin_name: str, admin_phone: str, members: list) -> None:
    org = os.environ.get("ROSEY_FLY_ORG", "personal")
    region = os.environ.get("ROSEY_FLY_REGION", "sjc")
    config_path = os.environ.get("ROSEY_HOUSEHOLD_CONFIG", str(DEFAULT_HOUSEHOLD_CONFIG))
    source_image = os.environ.get("ROSEY_SOURCE_IMAGE") or _latest_image_of(
        os.environ.get("ROSEY_TEMPLATE_APP", "rosey-template")
    )

    if not Path(config_path).exists():
        raise RuntimeError(f"household config not found: {config_path}")

    secrets_kv = _collect_secrets(admin_name, admin_phone, members)

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


def _collect_secrets(admin_name: str, admin_phone: str, members: list) -> dict:
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
    secrets_kv["HOUSEHOLD_TOML"] = _render_household_toml(admin_name, admin_phone, members)
    return secrets_kv


def _render_household_toml(admin_name: str, admin_phone: str, members: list) -> str:
    blocks = [
        'shopping_cadence = "weekly"',
        "",
        "[[members]]",
        f"name = {_toml_str(admin_name)}",
        f"phone = {_toml_str(admin_phone)}",
        'notes = ""',
    ]
    for m in members:
        blocks.extend([
            "",
            "[[members]]",
            f"name = {_toml_str(m['name'])}",
            f"phone = {_toml_str(m['phone'])}",
            'notes = ""',
        ])
    return "\n".join(blocks) + "\n"


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
    """Send a Telegram welcome to the admin and any pre-listed members."""
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
    body = _welcome_body(name, others)
    if not identifier.startswith("tg:"):
        log.warning("welcome skipped — non-Telegram identifier %s", identifier)
        return
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        log.warning("welcome skipped — TELEGRAM_BOT_TOKEN missing")
        return

    import json as _json
    import urllib.error
    import urllib.request

    chat_id = int(identifier[len("tg:"):])
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = _json.dumps({"chat_id": chat_id, "text": body[:4096]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("welcome sent to tg:%s", chat_id)
            else:
                log.warning("welcome status=%s for tg:%s", resp.status, chat_id)
    except urllib.error.HTTPError as e:
        log.error("welcome failed for tg:%s status=%s body=%s",
                  chat_id, e.code, e.read().decode("utf-8", errors="replace")[:300])
    except Exception:
        log.exception("welcome failed for tg:%s", chat_id)
