"""DB access layer for the router.

Uses SQLAlchemy 2.0 Core (not ORM) so we can write plain SQL while still
swapping SQLite (local) for Postgres (prod) via DATABASE_URL.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

DEFAULT_URL = "sqlite:///router.db"
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_engine(url: Optional[str] = None) -> Engine:
    """Build the SQLAlchemy engine. SQLite gets a StaticPool so the same
    connection is shared across threads — needed because the background
    provisioning thread also touches the DB."""
    url = url or os.environ.get("DATABASE_URL", DEFAULT_URL)
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def init_db(engine: Engine) -> None:
    """Create tables if they don't exist. Idempotent."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- households ---------------------------------------------------------

def create_household(engine: Engine, fly_app_name: str, status: str = "active") -> str:
    household_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO households (id, fly_app_name, status) "
                "VALUES (:id, :name, :status)"
            ),
            {"id": household_id, "name": fly_app_name, "status": status},
        )
    return household_id


def household_count(engine: Engine) -> int:
    with engine.begin() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM households")).scalar() or 0


def get_household(engine: Engine, household_id: str) -> Optional[dict]:
    with engine.begin() as conn:
        row = (
            conn.execute(
                text("SELECT id, fly_app_name, status FROM households WHERE id = :id"),
                {"id": household_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


# --- members ------------------------------------------------------------

def add_member(engine: Engine, phone: str, household_id: str, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO members (phone, household_id, name) "
                "VALUES (:p, :hid, :n)"
            ),
            {"p": phone, "hid": household_id, "n": name},
        )


def lookup_household(engine: Engine, phone: str) -> Optional[dict]:
    """Return {phone, household_id, fly_app_name, status, name} for a member.

    `phone` here is the lookup key but also reflected back in the result so
    callers (e.g. invite-code creator) can persist the identifier.
    """
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT m.phone, m.household_id, m.name, h.fly_app_name, h.status "
                    "FROM members m JOIN households h ON h.id = m.household_id "
                    "WHERE m.phone = :p"
                ),
                {"p": phone},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


# --- onboarding sessions ------------------------------------------------

def get_onboarding(engine: Engine, phone: str) -> Optional[dict]:
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT state, data, updated_at "
                    "FROM onboarding_sessions WHERE phone = :p"
                ),
                {"p": phone},
            )
            .mappings()
            .first()
        )
        if not row:
            return None
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result


def upsert_onboarding(engine: Engine, phone: str, state: str, data: dict) -> None:
    payload = json.dumps(data)
    now = _now_iso()
    with engine.begin() as conn:
        # Portable upsert: delete then insert. Both rows live in a transaction.
        conn.execute(text("DELETE FROM onboarding_sessions WHERE phone = :p"), {"p": phone})
        conn.execute(
            text(
                "INSERT INTO onboarding_sessions (phone, state, data, created_at, updated_at) "
                "VALUES (:p, :s, :d, :now, :now)"
            ),
            {"p": phone, "s": state, "d": payload, "now": now},
        )


def delete_onboarding(engine: Engine, phone: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM onboarding_sessions WHERE phone = :p"), {"p": phone})


# --- invite codes ------------------------------------------------------

def create_invite_code(
    engine: Engine,
    code: str,
    household_id: str,
    created_by: str,
    invitee_name: str,
    ttl_days: int = 7,
) -> None:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=ttl_days)
    ).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invite_codes "
                "(code, household_id, created_by, invitee_name, expires_at) "
                "VALUES (:c, :h, :b, :n, :e)"
            ),
            {"c": code, "h": household_id, "b": created_by, "n": invitee_name, "e": expires_at},
        )


def lookup_invite_code(engine: Engine, code: str) -> Optional[dict]:
    """Return invite code row if it exists, isn't expired, isn't used.
    Returns None for invalid / expired / already-used codes."""
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT code, household_id, invitee_name, expires_at, used_at "
                    "FROM invite_codes WHERE code = :c"
                ),
                {"c": code},
            )
            .mappings()
            .first()
        )
    if not row:
        return None
    d = dict(row)
    if d["used_at"]:
        return None
    if d["expires_at"] < datetime.now(timezone.utc).isoformat():
        return None
    return d


def mark_invite_used(engine: Engine, code: str, used_by: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE invite_codes SET used_at = :now, used_by = :u "
                "WHERE code = :c"
            ),
            {"now": _now_iso(), "u": used_by, "c": code},
        )
