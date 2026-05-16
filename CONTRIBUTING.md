# Contributing to Rosey

This is a small project. The bar for contributions is "does it make Rosey
useful to one more household without breaking the existing ones." That's it.

## Local dev

```bash
git clone https://github.com/aloktiagi/rosey
cd rosey
python3.11 -m venv .venv
.venv/bin/pip install -e '.[telegram]'

cp .env.example .env  # fill in ANTHROPIC_API_KEY + TELEGRAM_BOT_TOKEN
.venv/bin/python -m telegram_bot
```

For the SaaS router, see `router/README.md`.

## Formatting & linting

We use [ruff](https://docs.astral.sh/ruff/) for both formatting and a
conservative lint set. Set up the pre-commit hook once and it'll run
automatically on every `git commit`:

```bash
.venv/bin/pip install pre-commit
.venv/bin/pre-commit install
```

To run on the whole tree manually:

```bash
.venv/bin/pre-commit run --all-files
# or directly:
.venv/bin/ruff check --fix && .venv/bin/ruff format
```

Config lives in `pyproject.toml` under `[tool.ruff]`. CI re-runs the
same checks on every PR, so a forgotten `pre-commit install` won't slip
through — but it's nicer to catch it locally.

## Running tests

There's no full agent test (it makes live API calls). When changing
`agent.py`, the practical test is to run it locally with a real key and
message the bot from Telegram.

## Extension points

The codebase is organized so that the three things people most often want
to extend are isolated.

### 1. Add a new tool the agent can use

The agent's tool list lives in `tools.py`:

```python
# tools.py
def default_tools(memory) -> list[dict]:
    return [
        memory.to_dict(),
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ]
```

To add a server-side tool (e.g. code execution), append it here.
Claude is told about each tool and uses them autonomously. Catalog at
[platform.claude.com → Tool Use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview).

For *client-side* tools (where you implement the function yourself —
e.g. "send a calendar event," "post to Slack"), you'd subclass
`memory_tool.py` as a model and dispatch the tool call in the agent
loop. There's not yet a clean plugin system; for now, modify `agent.py`
directly.

### 2. Add a new slash command

For commands that should bypass the agent (`/feedback`, `/help`, `/leave`),
intercept them in the inbound channel before calling `handle_message`.
The pattern lives in `router/app.py`'s `/telegram` handler — match on the
prefix, do something custom, return a 200. Replicate it in
`telegram_bot.py`'s `_on_text` for the single-tenant deployment.

### 3. Add a new messaging channel (Discord, Slack, iMessage…)

Two things to wire up:

**Inbound:** an entry point that calls
`handle_message(sender_id, body)` with whatever identifier scheme makes
sense for your channel. Examples:

- Telegram: `tg:12345678`
- Discord: `discord:user_id`
- Slack: `slack:U12345`

The agent treats this identifier as opaque — it's used as the key for
thread state (`memories/threads/<id>.md`) and household lookup. The
prefix lets the dispatcher pick the right outbound channel.

**Outbound:** add a `send_<channel>(target, body) -> bool` function to
`channels.py` and add a branch in `channels.send()`:

```python
# channels.py
def send(identifier: str, body: str) -> bool:
    if identifier.startswith("tg:"):
        return send_telegram(identifier[len("tg:"):], body)
    if identifier.startswith("discord:"):
        return send_discord(identifier[len("discord:"):], body)
    log.warning("unknown identifier scheme: %s", identifier)
    return False
```

That's the only place outbound dispatch lives — `reminders.py` and
`summary.py` (and any future fan-out) all go through `channels.send`.

**Roster recognition:** update `roster.py:members()` to accept the new
prefix in `household.md` lines:

```python
# roster.py — in members()
if ident.startswith("tg:") or ident.startswith("discord:"):
    out.append(Member(name=name, identifier=ident, notes=notes))
```

Now `household.md` entries like `- Alex (discord:123456789)` route
end-to-end with no other changes.

### 4. Add a new memory file convention

The agent organizes `/memories/` itself based on its system prompt. To
nudge it toward a new file (say `pets.md` for tracking pet stuff), add
a sentence to `agent.py`'s `SYSTEM_PROMPT`:

```python
"""...
Your memory directory at /memories is the household's durable state.
Organize it however helps you — likely shape:
- household.md, groceries/list.md, pantry.md, reminders.md
- pets.md (vaccination dates, vet contacts, food brand, etc.)
- knowledge/<topic>.md
..."""
```

That's enough for Claude to read/write the file appropriately. No code
changes needed.

## Style

- Plain Python, no frameworks beyond Flask (HTTP) and SQLAlchemy (router DB).
- No type-checker required, but type hints on public functions are appreciated.
- Comments only when the *why* is non-obvious. Don't narrate the *what*.
- Keep dependencies small. New dependencies need a one-line justification in the PR.

## Privacy & safety

- Never log message bodies, only metadata. Look for `len=%d` patterns in
  existing log lines for the convention.
- Don't add analytics. The privacy promise on the website is intentional.
- New outbound channels should respect the household roster (don't message
  anyone not in `household.md`).

## License

MIT — see [`LICENSE`](./LICENSE). By submitting a PR you agree that your
contribution is licensed the same way.
