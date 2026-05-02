# Rosey

A small, shared assistant your household talks to over chat.

> Lists, reminders, family knowledge, web research, voice notes — all by text.
> Backed by Claude with the [Memory Tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool), so the agent organizes everything as plain markdown files you can read, back up, or grep.

```
You:    we're out of milk
Rosey:  added 🥛 (Dairy)

You:    what's the wifi password again?
Rosey:  goldfinch42

You:    find a plumber near us
Rosey:  3 nearby:
        · Bay Plumbing — (650) 555-0123
        · Reliable Rooter — (650) 555-0177
        · Pipemasters — (650) 555-0144

You:    remind me Friday at 9am to take out the trash
Rosey:  ✓ I'll nudge you at 09:00 Fri.
```

---

## Quickstart (self-host with Telegram, ~10 min)

The fastest setup. Runs anywhere — your laptop, a Raspberry Pi, or a Fly.io VM.

**1. Make a Telegram bot.**
Open Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
Rosey
my_household_rosey_bot
```

Save the token he gives you. Looks like `1234567890:AAEh...`.

**2. Get an Anthropic API key.**
[console.anthropic.com](https://console.anthropic.com) → API Keys → Create Key. Add a few dollars of credit.

**3. Clone & configure.**

```bash
git clone https://github.com/aloktiagi/rosey
cd rosey
python3.11 -m venv .venv
.venv/bin/pip install -e '.[telegram]'

cp .env.example .env
# Fill in:
#   ANTHROPIC_API_KEY=sk-ant-...
#   TELEGRAM_BOT_TOKEN=1234567890:AAEh...
#   SCHEDULER_TZ=America/Los_Angeles  (your timezone)
#   MEMORY_ROOT=./memories
```

**4. Run it.**

```bash
.venv/bin/python -m telegram_bot
```

That's it. Open Telegram, find your bot by username, hit `/start`. Tell each family member to do the same — the bot will tell them their `chat_id`, and you paste each one into `memories/household.md`:

```markdown
# Household

Members:
- Alex (tg:12345678)
- Sam (tg:87654321)

Shopping cadence: weekly
```

Once they're listed, every message they send goes through the agent. (If `household.md` is empty, the bot trusts everyone — fine for an initial test.)

---

## Quickstart (self-host on Fly.io, persistent + free-ish)

Same as above, but instead of running on your laptop, deploy to Fly so the bot is always on. ~$5/mo for the VM, free for the volume.

**1. Install the Fly CLI** ([docs](https://fly.io/docs/hands-on/install-flyctl/)).

**2. Provision and deploy.**

```bash
fly launch --no-deploy --copy-config --name <your-bot-name>
fly volumes create memory_data --size 1 --region <your-region>

fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  TELEGRAM_BOT_TOKEN=1234567890:AAEh... \
  TELEGRAM_WEBHOOK_URL=https://<your-bot-name>.fly.dev \
  -a <your-bot-name>

fly deploy
```

The presence of `TELEGRAM_WEBHOOK_URL` switches the bot from polling to webhook mode automatically. The bot is now reachable through Fly's load balancer; Telegram delivers updates directly.

(Optional) If you want voice-note transcription, add `OPENAI_API_KEY` to the secrets above and set `pip install -e '.[telegram]'` deps include `requests`. Telegram → Whisper → agent.

---

## Architecture

```
[Family member's phone] ──▶ Telegram ──▶ telegram_bot.py
                                              │
                                              ├─ memory tool   → /memories/*.md (on disk)
                                              ├─ web_search    → live info
                                              ├─ web_fetch     → URL contents
                                              └─ Claude (Sonnet) does the reasoning
                                              │
                                              ▼
                                   reply via Telegram bot API
```

Single-tenant by default: one bot, one household, one set of API keys. Memory is plain markdown files in `MEMORY_ROOT/memories/` — back them up by copying the directory.

**File layout** (lives at the repo root, will be moved into `src/` over time):

| File | Purpose |
|---|---|
| `agent.py` | Tool loop, system prompts, thread state |
| `memory_tool.py` | File-backed memory tool (subclass of SDK's `BetaAbstractMemoryTool`) with size caps + path-traversal guards |
| `telegram_bot.py` | Telegram polling/webhook adapter (single-tenant entrypoint) |
| `app.py` | Flask app for the multi-tenant household VM (used by the SaaS router; ignore for self-hosting) |
| `channels.py` | Outbound dispatch by identifier prefix (e.g. `tg:` → Telegram bot API) |
| `reminders.py` | 1-minute polling job that fires reminders by reading `/memories/reminders.md` and dispatching via `channels.send` |
| `summary.py` | Saturday-morning digest |
| `household.py` | Renders `household.md` from a TOML config |
| `transcribe.py` | OpenAI Whisper for voice notes |

---

## Optional: Multi-tenant SaaS router

The `router/` directory contains a separate Flask service for running Rosey as a service: one public endpoint, many households, each with its own dedicated Fly VM and isolated memory. This is what's deployed at `rosey-router.fly.dev`.

It's not required for self-hosting. The single-tenant `telegram_bot.py` above is the recommended starting point.

If you want to run a multi-household service yourself, see [`router/README.md`](./router/README.md). Be aware it pairs with `flyctl` shell-out and hard-codes Fly.io as the orchestration backend; you'd need to rewrite `router/provisioning.py` for other clouds.

---

## Extending Rosey

Three common extension points, each with a short pattern:

**Add a new agent capability.** Server-side tools (web_search, web_fetch) are declared in `agent.py`'s `tools` list. Add a new entry from [Anthropic's tool catalog](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview) — e.g., `code_execution_20260120` — and Claude can use it on every turn. No code changes elsewhere.

**Add a new slash command** (like `/feedback`). Intercept it before calling `handle_message`. Pattern in `router/app.py`'s `_is_feedback` — match the prefix in the inbound handler, do something custom, return early.

**Add a new messaging channel.** Implement an inbound handler that calls `handle_message(sender_id, body)` with whatever identifier scheme makes sense for your channel (e.g. `discord:user_id`, `slack:U12345`). Add a matching `_send_<channel>` function in `reminders.py` for outbound, and update the prefix dispatch in `_send_reminder`. The agent itself doesn't care about channel.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for more detail.

---

## License

[MIT](./LICENSE).
