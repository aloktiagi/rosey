# Rosey

Rosey is a self-hosted household assistant that lives in the chat apps your
family already uses. Text it a question, a reminder, a shopping-list update,
or a voice note, and it keeps the shared context in plain markdown files that
you own.

Rosey is built for small households that want practical coordination without
adding another app: reminders, lists, household facts, web research, and
weekly summaries from Telegram today, with optional WhatsApp and Alexa entry
points.

```text
You:    we're out of milk
Rosey:  Added milk to groceries.

You:    what's the wifi password?
Rosey:  The guest network password is goldfinch42.

You:    remind me Friday at 9am to take out the trash
Rosey:  I'll remind you Friday at 9:00 AM.
```

## Features

- Chat-first interface through Telegram, plus optional WhatsApp and Alexa handlers.
- Claude-powered agent loop using Anthropic's local filesystem memory tool.
- Durable household memory stored as markdown in the resolved memories directory.
- Reminder scheduler with persistent APScheduler jobs and escalation support.
- Voice-note transcription through OpenAI Whisper when `OPENAI_API_KEY` is set.
- Fly.io-ready Docker deployment with a persistent volume for memory and scheduler state.
- Optional multi-tenant router in `router/` for running Rosey as a hosted service.

## Status

This project is early and intentionally small. Telegram single-household hosting is
the recommended path. WhatsApp, Alexa, and the SaaS router are present for people
who want to experiment, but they require more service-specific setup.

## Requirements

- Python 3.11 for local development.
- An [Anthropic API key](https://platform.claude.com/settings/workspaces/default/keys).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A Fly.io account and [`flyctl`](https://fly.io/docs/flyctl/install/) for production hosting.
- Optional: an OpenAI API key for voice-note transcription.

## Quick Start: Local Telegram Bot

Local mode uses Telegram long polling, so you do not need a public webhook URL.

```bash
git clone https://github.com/aloktiagi/rosey.git
cd rosey

python3.11 -m venv .venv
.venv/bin/pip install -e '.[telegram]'

cp .env.example .env
```

Edit `.env`:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=1234567890:AAEh...
MEMORY_ROOT=./memories
SCHEDULER_TZ=America/Los_Angeles
```

Start the bot:

```bash
.venv/bin/python -m telegram_bot
```

Message your bot in Telegram. Send `/start` first to get your Telegram chat ID,
then add allowed household members to `memories/household.md`:

```markdown
# Household

Members:
- Alex (tg:12345678)
- Sam (tg:87654321)
```

If `household.md` is missing or has no members, Rosey accepts messages from
anyone who can reach the bot. That is useful for first setup, but not recommended
for a real deployment.

## Deploy on Fly.io

The production container runs a Quart server with routes for Telegram,
WhatsApp, Alexa, and health checks. Telegram webhook mode is enabled when
`TELEGRAM_WEBHOOK_URL` is set.

```bash
fly launch --no-deploy --copy-config --name <your-app-name>
fly volumes create memory_data --size 1 --region <your-region> -a <your-app-name>

python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Use the generated token as `TELEGRAM_WEBHOOK_SECRET`:

```bash
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  TELEGRAM_BOT_TOKEN=1234567890:AAEh... \
  TELEGRAM_WEBHOOK_URL=https://<your-app-name>.fly.dev \
  TELEGRAM_WEBHOOK_SECRET=<generated-secret> \
  -a <your-app-name>

fly deploy -a <your-app-name>
```

Optional voice transcription:

```bash
fly secrets set OPENAI_API_KEY=sk-... -a <your-app-name>
```

The included `fly.toml` mounts `/data`, sets `MEMORY_ROOT=/data/memories`, and
keeps at least one machine running so reminders can fire on time.

## Configuration

| Variable | Required | Purpose |
|---|---:|---|
| `ANTHROPIC_API_KEY` | Yes | Claude API key for the agent. |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from BotFather. |
| `MEMORY_ROOT` | Yes | Directory where Rosey stores markdown memory files. |
| `SCHEDULER_TZ` | Recommended | Timezone for reminders and summaries. |
| `TELEGRAM_WEBHOOK_URL` | Production | Base HTTPS URL for webhook mode. |
| `TELEGRAM_WEBHOOK_SECRET` | Production | Secret token checked on Telegram webhook requests. |
| `OPENAI_API_KEY` | Optional | Enables voice-note transcription. |
| `BAILEYS_MODE` | Optional | Set to `on` to run the WhatsApp Baileys sidecar. |
| `BAILEYS_BRIDGE_SECRET` | Optional | Shared secret between the Baileys sidecar and Python server. |

See `.env.example` for a starter local environment file.

## How It Works

```text
Telegram / WhatsApp / Alexa
          |
          v
    server.py / telegram_bot.py
          |
          v
       agent.py
          |
          +--> memory_tool.py  ->  memories/*.md
          +--> tools.py        ->  web_search and web_fetch
          +--> scheduler.py    ->  persistent reminder jobs
          |
          v
   reply through channels.py
```

The default deployment is single-tenant: one bot, one household, one memory
directory. `MEMORY_ROOT` can point directly at `memories/` or at its parent
directory; `paths.py` normalizes both forms. The router service in `router/`
is a separate app for provisioning many isolated household VMs.

## Repository Layout

| Path | Purpose |
|---|---|
| `agent.py` | Claude tool loop, system prompt, and message handling. |
| `server.py` | Production Quart app for webhooks and health checks. |
| `telegram_bot.py` | Local polling mode and Telegram handler functions. |
| `scheduler.py` | Persistent reminder scheduling and acknowledgement tracking. |
| `channels.py` | Outbound dispatch to Telegram, WhatsApp, and Baileys. |
| `memory_tool.py` | File-backed memory tool with path and size guards. |
| `roster.py` | Parses `household.md` members and identifiers. |
| `reminder_format.py` | Shared parser for reminder markdown lines. |
| `baileys/` | Optional WhatsApp MultiDevice sidecar. |
| `router/` | Optional multi-tenant router service. |
| `website/` | Static marketing site. |
| `scripts/` | Deployment and smoke-test helpers. |

## Development

Run the focused test scripts directly:

```bash
.venv/bin/python test_gate_unified.py
.venv/bin/python test_reminder_fixes.py
.venv/bin/python test_recurring_reminders.py
.venv/bin/python scripts/test_escalation_ladder.py
```

Check syntax:

```bash
.venv/bin/python -m compileall -q .
```

There is no complete offline test for `agent.py` because the real agent path
makes live API calls. For agent changes, run locally with a test Telegram bot
and watch the logs.

## Privacy Notes

Rosey stores household state in files you control. Keep `memories/`, `.env`,
local database files, and deployment secrets out of git. The repository ignores
those paths by default.

The code follows a metadata-only logging convention: log sender identifiers and
message lengths, not message bodies. Please preserve that convention in new
channels or tools.

## Costs

A small always-on Fly.io machine with a 1 GB volume is usually around $5/month.
Anthropic API usage varies by household; a modest family deployment is commonly
in the low tens of dollars per month. Check current model pricing before relying
on any estimate.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for extension points and project
conventions.

## License

[MIT](./LICENSE)
