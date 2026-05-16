-- Rosey router tenant schema. Works on SQLite (local dev) and Postgres (prod).

CREATE TABLE IF NOT EXISTS households (
    id              TEXT PRIMARY KEY,
    fly_app_name    TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,
    -- chat_id of the Telegram group the bot has been added to (negative
    -- for groups). NULL until the user creates one via the "Create family
    -- group" inline button at the end of onboarding.
    group_chat_id   TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS members (
    -- For active members: "tg:<chat_id>".
    -- For pre-rostered placeholders waiting on first contact: "pending:<uuid>".
    phone           TEXT PRIMARY KEY,
    household_id    TEXT NOT NULL,
    name            TEXT NOT NULL,
    -- Lowercased Telegram username without "@". Populated for pending rows
    -- and (when known) for active rows. Used for pending → active upgrade
    -- when a username-bearing user first messages the bot.
    tg_username     TEXT,
    email           TEXT,
    joined_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
);

-- Indexes live in db._migrate so they're created AFTER all columns are
-- present (legacy DBs upgrade via ALTER TABLE first).

CREATE TABLE IF NOT EXISTS onboarding_sessions (
    phone           TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    data            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS invite_codes (
    code            TEXT PRIMARY KEY,
    household_id    TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    invitee_name    TEXT,
    expires_at      TEXT NOT NULL,
    used_at         TEXT,
    used_by         TEXT,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
);
