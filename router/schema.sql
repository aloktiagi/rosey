-- Rosey router tenant schema. Works on SQLite (local dev) and Postgres (prod).

CREATE TABLE IF NOT EXISTS households (
    id              TEXT PRIMARY KEY,
    fly_app_name    TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS members (
    phone           TEXT PRIMARY KEY,
    household_id    TEXT NOT NULL,
    name            TEXT NOT NULL,
    joined_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_members_household ON members(household_id);

CREATE TABLE IF NOT EXISTS onboarding_sessions (
    phone           TEXT PRIMARY KEY,
    state           TEXT NOT NULL,
    data            TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_onb_updated ON onboarding_sessions(updated_at);

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

CREATE INDEX IF NOT EXISTS idx_invite_household ON invite_codes(household_id);
