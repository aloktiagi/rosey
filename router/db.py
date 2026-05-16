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

from sqlalchemy import create_engine, inspect, text
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
    """Create tables if they don't exist, then run idempotent migrations.

    Migrations exist because ALTER TABLE ADD COLUMN isn't IF-NOT-EXISTS on
    SQLite (or portable to Postgres) — we use the SQLAlchemy inspector to
    detect missing columns and add them only when needed."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with engine.begin() as conn:
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    _migrate(engine)


def _migrate(engine: Engine) -> None:
    """Add any columns/indexes that exist in the canonical schema but not
    yet on disk. Safe to run on every startup."""
    insp = inspect(engine)

    def has_column(table: str, column: str) -> bool:
        try:
            cols = {c["name"] for c in insp.get_columns(table)}
        except Exception:
            return False
        return column in cols

    # ALTER TABLE: add columns that don't exist on legacy DBs.
    alters: list[str] = []
    if has_column("households", "id") and not has_column("households", "group_chat_id"):
        alters.append("ALTER TABLE households ADD COLUMN group_chat_id TEXT")
    if has_column("members", "phone"):
        if not has_column("members", "tg_username"):
            alters.append("ALTER TABLE members ADD COLUMN tg_username TEXT")
        if not has_column("members", "email"):
            alters.append("ALTER TABLE members ADD COLUMN email TEXT")

    with engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))
        # Indexes — every CREATE INDEX is idempotent. Run unconditionally so
        # they exist on both fresh and legacy DBs.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_members_household "
            "ON members(household_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_households_group "
            "ON households(group_chat_id)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_members_tg_username "
            "ON members(tg_username)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_onb_updated "
            "ON onboarding_sessions(updated_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_invite_household "
            "ON invite_codes(household_id)"
        ))


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
                text(
                    "SELECT id, fly_app_name, status, group_chat_id "
                    "FROM households WHERE id = :id"
                ),
                {"id": household_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


def set_group_chat_id(engine: Engine, household_id: str, group_chat_id: int) -> None:
    """Link a Telegram group chat to this household. Called when the bot
    is added to a new group by an onboarded user."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE households SET group_chat_id = :gid WHERE id = :hid"
            ),
            {"gid": str(group_chat_id), "hid": household_id},
        )


def lookup_household_by_group(engine: Engine, group_chat_id: int) -> Optional[dict]:
    """Find the household tied to a given Telegram group chat_id. Used for
    routing inbound group messages (chat_id < 0 means group).
    """
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT id, fly_app_name, status, group_chat_id "
                    "FROM households WHERE group_chat_id = :gid"
                ),
                {"gid": str(group_chat_id)},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


# --- members ------------------------------------------------------------

def add_member(
    engine: Engine,
    phone: str,
    household_id: str,
    name: str,
    tg_username: Optional[str] = None,
    email: Optional[str] = None,
) -> None:
    """Insert a fully-active member. ``phone`` is the identifier, typically
    ``tg:<chat_id>``. For pre-rostered placeholders, use ``add_pending_member``
    instead so the identifier is generated correctly."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO members (phone, household_id, name, tg_username, email) "
                "VALUES (:p, :hid, :n, :u, :e)"
            ),
            {
                "p": phone,
                "hid": household_id,
                "n": name,
                "u": tg_username.lower() if tg_username else None,
                "e": email,
            },
        )


def add_pending_member(
    engine: Engine,
    household_id: str,
    name: str,
    tg_username: Optional[str] = None,
) -> str:
    """Insert a placeholder member who hasn't messaged the bot yet.

    Returns the synthetic identifier (``pending:<uuid>``) so callers can
    reference the row before the member is upgraded.
    """
    placeholder = f"pending:{uuid.uuid4().hex[:8]}"
    add_member(
        engine,
        phone=placeholder,
        household_id=household_id,
        name=name,
        tg_username=tg_username,
    )
    return placeholder


def lookup_pending_by_username(engine: Engine, tg_username: str) -> Optional[dict]:
    """Find a pre-rostered placeholder member by Telegram username.

    Returns None if no match, or if multiple pending rows share the same
    username across different households (ambiguous → treat as no match
    and let the user go through normal onboarding / invite-code).
    """
    if not tg_username:
        return None
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT m.phone, m.household_id, m.name, h.fly_app_name, h.status "
                    "FROM members m JOIN households h ON h.id = m.household_id "
                    "WHERE m.tg_username = :u AND m.phone LIKE 'pending:%'"
                ),
                {"u": tg_username.lower()},
            )
            .mappings()
            .all()
        )
    if len(rows) != 1:
        return None
    return dict(rows[0])


def upgrade_pending_member(
    engine: Engine, old_phone: str, new_phone: str
) -> None:
    """Promote a placeholder member to active by swapping the identifier
    from ``pending:<uuid>`` to ``tg:<chat_id>``."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE members SET phone = :new WHERE phone = :old"),
            {"new": new_phone, "old": old_phone},
        )


def lookup_household(engine: Engine, phone: str) -> Optional[dict]:
    """Return {phone, household_id, name, fly_app_name, status, group_chat_id}
    for a member, or None.

    `phone` here is the lookup key but also reflected back in the result so
    callers (e.g. invite-code creator) can persist the identifier.
    """
    with engine.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT m.phone, m.household_id, m.name, "
                    "h.fly_app_name, h.status, h.group_chat_id "
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
