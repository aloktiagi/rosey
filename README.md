# Rosey

A shared assistant for your whole family, reachable from the chat apps you already use.

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

## Requirements

- A **Telegram bot token** — get one from [@BotFather](https://t.me/BotFather) (free, ~30s).
- An **Anthropic API key** — [platform.claude.com → API Keys](https://platform.claude.com/settings/workspaces/default/keys).
- A **Fly.io account** + **flyctl** installed: <https://fly.io/docs/hands-on/install-flyctl/>. (Or pick a different host — see [Costs](#costs).)
- **git** for cloning the repo.
- *(Optional)* An **OpenAI API key** if you want voice-note transcription via Whisper.

---

## Setup

About an hour end-to-end if you've shipped a webhook before.

**1. Clone the repo.**

```bash
git clone https://github.com/atandon1994/rosey && cd rosey
```

**2. Get a Telegram bot token from @BotFather.**

Open Telegram, message [@BotFather](https://t.me/BotFather):

```
/newbot
Rosey
my_household_rosey_bot
```

Save the token he gives you. It looks like `1234567890:AAEh...`.

**3. Get an Anthropic API key.**

Go to [platform.claude.com → API Keys](https://platform.claude.com/settings/workspaces/default/keys), create a key, and add a few dollars of credit. (See [Costs](#costs) for what to expect.)

**4. Deploy to Fly.io.**

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

The `TELEGRAM_WEBHOOK_URL` secret switches the bot from polling to webhook mode automatically. Telegram delivers updates straight through Fly's load balancer.

*(Optional)* Add `OPENAI_API_KEY=...` to the secrets above for voice-note transcription via Whisper.

**5. Edit `memories/household.md` with your family.**

Have each person send `/start` to the bot once — she'll reply with their Telegram chat ID. Paste them into the file:

```markdown
# Household

Members:
- Alex (tg:12345678)
- Sam (tg:87654321)
```

(If `household.md` is empty, the bot trusts everyone — fine for an initial test.)

**6. Text your bot.**

Open Telegram, find your bot by username, send her a message. She'll respond.

That's the full Telegram path. WhatsApp and Alexa channels are optional bolt-ons; their setup lives in the repo's `docs/` (link will work once the docs are published).

---

## Costs

Two line items.

**Hosting.** Fly.io's smallest shared-CPU VM with a 1GB persistent volume runs about **$5/mo**. The volume is free below 3GB; the VM is the line item.

**Anthropic API.** Usage-based. For a typical family with 20–40 messages a day across all members, plan on **$5–15/mo**. Prompt caching covers most of the system-prompt overhead, so the bulk of your bill is output tokens. Current rates: [anthropic.com/pricing](https://www.anthropic.com/pricing).

**Total: roughly $10–20/mo** for a moderately active family. Heavier use (lots of reminders, lots of members, lots of web research) can push this to $20–40/mo.

### Alternatives that lower or eliminate the hosting bill

- **Raspberry Pi at home** — $0/mo ongoing once you've bought the Pi. You'll need a stable address for Telegram's webhook; [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) and [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) are the easiest paths.
- **Hetzner / DigitalOcean / Linode VPS** — $4–6/mo for a comparable VM. Hetzner is the cheapest of the three.
- **Render / Railway** — ~$7/mo. Similar developer experience to Fly.
- **An existing home server or NAS** — $0/mo if you already have one running. Docker-Compose works.

### Alternatives that lower the API bill

- **Switch the model.** Edit `agent.py` to use Claude Haiku instead of Sonnet — significantly cheaper, with a real (but often acceptable) capability drop for everyday family tasks.
- **Cap tool iterations.** `MAX_TOOL_ITERATIONS` in `agent.py` controls how many tool calls Rosey makes per message. Lowering it reduces worst-case cost when the agent thrashes.

---

## Alternative: run locally for testing

Skip Fly and run on your laptop or Raspberry Pi:

```bash
git clone https://github.com/atandon1994/rosey && cd rosey
python3.11 -m venv .venv
.venv/bin/pip install -e '.[telegram]'

cp .env.example .env
# Fill in:
#   ANTHROPIC_API_KEY=sk-ant-...
#   TELEGRAM_BOT_TOKEN=1234567890:AAEh...
#   SCHEDULER_TZ=America/Los_Angeles  (your timezone)
#   MEMORY_ROOT=./memories

.venv/bin/python -m telegram_bot
```

Uses Telegram's long-polling mode, so no public URL needed. Good for hacking on the agent locally; not recommended as a long-term host since the bot dies when your laptop sleeps.

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
