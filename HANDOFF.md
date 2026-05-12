# Rosey — handoff document

*For pasting at the start of a new Claude session so it can pick up where the previous one left off.*

---

## How to use this doc

You (Claude) are continuing work on a project called Rosey. Read this whole document before responding to the user's first message. It contains:

- Who the user is and how they prefer to work
- What Rosey is, current architecture, what's built vs not
- Active design decisions and what was just shipped
- Open threads waiting for a decision or build
- A short list of concrete next actions

The repo lives at `/Users/ankitpersonal/Documents/rosey/`. The user works on macOS and ships to Fly.io.

---

## The user

**Ankit Tandon** (`atandon1994@gmail.com`). Currently on paternity leave. Rosey was born out of the operational chaos of a newborn household — pediatrician follow-ups, feeding times, the cat's medication, the dog's vet, who's doing pickup — stuff that used to live in two heads and now needs to live somewhere addressable. He's building Rosey between feeds and intends to open-source it so other households benefit.

He's technical, comfortable with the full stack, and on the SF Bay peninsula. iPhone 13 Pro (relevant for WhatsApp/Baileys setup). Partner's name is Sunanda; baby is Maya. They use Telegram and WhatsApp as their family communication layer.

### How he likes to work

- **Direct and concise.** Prose over bullet-spam. Bullets only when they're actually a list of discrete items. Don't pad.
- **Honest pushback over sycophancy.** "Where do you want to push back?" is a normal close. If something is brittle, say so.
- **Options + your recommendation.** When there's a real choice, give him the trade-offs and tell him what you'd do. He'll make the call.
- **Flag things proactively.** "Two things worth flagging" patterns work well — surface follow-ups he didn't ask about but should know.
- **Don't ask for clarification you don't need.** If the spec is ambiguous, propose your interpretation explicitly and let him correct it, rather than asking 3 questions.
- **He drives the design; you implement and verify.** Don't unilaterally make architectural choices; do propose them concretely.
- **Tests are part of "done."** Real test scripts, not just "I think this works."

---

## What Rosey is

A self-hosted household assistant. The product framing (lifted from rosey.house): "a context layer for your home." Text Rosey from any phone — Telegram, WhatsApp DM, WhatsApp group, eventually Alexa — and it holds the small shared facts your household runs on, schedules reminders that escalate when no one acks, and keeps both adults synchronized on what's going on.

Key principles:

- **The chats you already have, not a new app.** Rosey joins existing Telegram/WhatsApp threads.
- **Self-hosted, not SaaS.** Runs on the user's Fly.io account, ~$5/mo. No lock-in, no account.
- **Memory belongs to the household.** All state in `/memories/*.md` on the user's volume.
- **Claude under the hood.** Currently `claude-sonnet-4-6` with memory tool + web search/fetch.

---

## Architecture

```
        Telegram webhook  ─┐
        WhatsApp Cloud API ─┤            ┌──→ /memories/*.md
        Baileys sidecar    ─┼─→ agent ──┤    (household.md, reminders.md,
        Alexa skill        ─┘   loop    │     knowledge/, threads/, ...)
                                         │
                                         └──→ scheduler ──→ APScheduler jobstore
                                                            (sqlite, on /data)
```

- **HTTP layer:** Quart (async Flask-shaped) served by Hypercorn on `:8080`.
- **Routes:** `/telegram`, `/whatsapp` (GET verify + POST events), `/whatsapp-baileys`, `/alexa`, `/health`.
- **Multi-runtime container:** Python 3.11 + Node 20 (for Baileys). One Dockerfile, one `scripts/start.sh` launches both processes side-by-side.
- **Agent:** Claude Sonnet 4.6, tools = memory + web_search + web_fetch.
- **Scheduler:** APScheduler with SQLAlchemy SQLite jobstore on Fly volume `/data/scheduler.db`.
- **Identifier scheme:** `tg:<chat_id>`, `wa:+<E164>`, `wa:group:<JID>`, `alexa:<amzn-id>`. Roster maps person → list of identifiers; a single `@Name` mention fans out to every channel.

---

## What's built and working

- **Telegram channel:** fully working in production on Fly. Webhook secret-token auth.
- **WhatsApp Cloud API (1:1):** working for any of the 5 verified test recipients. HTML stripped on outbound (Cloud API doesn't accept Telegram's `parse_mode=HTML`).
- **Baileys WhatsApp sidecar:** code complete, tested end-to-end in smoke tests, NOT YET DEPLOYED with a real account. Awaiting SIM card.
- **Alexa channel:** code complete, deployed; simulator showed `INVALID_RESPONSE` last we checked. Punted — unknown whether TLS or skill config.
- **Roster:** flexible parser. Accepts `- **Name** — tg:NNN, wa:+NNN`, legacy `Name (tg:NNN) — notes`, and markdown tables. Multi-identifier fan-out for `@Name` to all of that person's channels.
- **Reminders with per-addressee escalation ladder** (just shipped — see "recent work" below).
- **Memory:** custom `FileMemoryTool` subclass of Anthropic's `BetaLocalFilesystemMemoryTool` with 100KB per-file / 10MB total caps, plus a guard against empty `old_str`.
- **Knowledge INDEX.md pattern** (just shipped — agent maintains a catalog of `/memories/knowledge/*.md`).
- **Marketing site** at `/website/index.html` — single-file, OpenAI-style design, paired with a planned video demo. Uses placeholder repo URL `github.com/atandon1994/rosey`.

---

## File map (the ones that matter)

- `agent.py` — entry point: `handle_message(from_phone, body, origin_chat=...)`. Contains the main `SYSTEM_PROMPT` template (long; has every behavioral rule).
- `scheduler.py` — APScheduler integration. Public API: `start`, `shutdown`, `reconcile`, `mark_acked`, `find_task_by_chat_msg`, `recent_fires_for`, plus the four job targets `fire_one` / `escalate_one` / `fallback_one` / `miss_one`. State lives in markdown annotations on each reminder line, not in a side table — the file IS the audit trail.
- `reminder_format.py` — regex single source of truth for parsing reminder lines. `LINE_RE`, `MENTION_RE`, `FROM_RE`, `ID_RE`, `ESC_RE`, `MISS_RE`, `URG_RE`, `FB_RE`, plus annotation regexes.
- `roster.py` — `household.md` parser. `Member` dataclass, `members()`, `members_grouped_by_name()`.
- `channels.py` — outbound dispatch by identifier prefix. `send(ident, body)` routes to Telegram, WhatsApp Cloud API, or Baileys based on prefix + env (`BAILEYS_MODE`).
- `memory_tool.py` — `FileMemoryTool` with size caps.
- `whatsapp_handler.py` — Meta envelope parsing (Cloud API) AND Baileys event handling. `handle_event` for Cloud API; `handle_baileys_event` for the sidecar.
- `telegram_bot.py` — PTB handlers (`_on_start`, `_on_text`, `_on_voice`, `_on_status_command`) plus the fuzzy gate.
- `alexa_handler.py` — Alexa intent dispatcher.
- `gate.py` — fuzzy gate that decides whether to respond in group chats (don't interrupt unless addressed).
- `server.py` — Quart app, route registrations, lifecycle hooks.
- `baileys/index.js` — Node sidecar that speaks WhatsApp's MultiDevice protocol. Bridges to Python via loopback HTTP with `X-Bridge-Secret` header. Persists session to `/data/baileys-session`.
- `scripts/start.sh` — wrapper that launches both processes; `BAILEYS_MODE=off` skips the sidecar.
- `scripts/test_escalation_ladder.py` — 22-check smoke test for the new escalation ladder. All passing.
- `memories/` — the live data. `household.md`, `reminders.md`, `knowledge/*.md`, `threads/<id>.md`, `groceries/list.md`.
- `website/index.html` — marketing site.

---

## Recent session highlights (what was just shipped)

### 1. Per-addressee escalation ladder (the big one)

The reminder lifecycle used to be three jobs per reminder (`fire` / `escalate` / `miss`), all firing to the originating chat. It now fans out per-addressee with four tiers and an urgency preset.

**Per addressee:** `fire:<task_id>:<slug>` and `escalate:<task_id>:<slug>` — each addressee gets their own fire to their own channels, plus their own escalate (louder phrasing, same channels) if the tier has one.

**Per reminder:** `fallback:<task_id>` and `miss:<task_id>` — fallback pages a dynamically-resolved third party; miss is terminal logging.

**Urgency tiers** in `URGENCY_INTERVALS`:

| tier | escalate | fallback | miss |
|---|---|---|---|
| low | — | — | +1h (log only) |
| normal (default) | +15m | +45m | +2h |
| high | +3m | +10m | +30m |

Agent picks the tier at schedule time by writing `urg:low|normal|high` on the reminder line. System prompt teaches the rule: *high* for medication/pickup/appointments/explicit-important, *low* only when the user explicitly says "don't chase me," *normal* default with a lean toward escalation. Per-line `esc:Nm` / `miss:Nh` still override the preset.

**Dynamic fallback resolution** (in `_resolve_fallback_recipient`, at fire time):

1. Explicit `fb:Name` tag on the line
2. Owner of the `from:` chat if a different person from any addressee
3. Next household member by roster order, excluding addressees
4. Skip silently if nothing resolves

**Cross-ladder ack cancellation comes for free** from the existing annotation pattern: when any addressee acks, the agent appends `(acked by Name at T)` to the line via `str_replace`. Every downstream job reads the line at fire time and self-skips if `(acked` is present. One ack kills every pending ladder for every addressee, no explicit cancellation needed.

**Casual ack disambiguation:** new `recent_fires_for(identifier, within_minutes)` in `scheduler.py` returns un-acked reminders fired to this user in the last N minutes. Injected into every prompt as a `<recent_fires>` block so when the user replies "ok" / "yep" / "done," the agent has a concrete handle for the most recent reminder.

**Snooze pattern:** documented in the system prompt as "ack the old line, write a new line at the snoozed time." Two-step, audit-trail-preserving.

Tests at `scripts/test_escalation_ladder.py` — 22 checks across 6 scenarios, all passing. Includes per-addressee fan-out, urgency interval correctness, ack self-skip, fallback resolution, recent-fires lookup, snooze pattern.

### 2. Knowledge INDEX.md pattern

`/memories/knowledge/INDEX.md` is a catalog of every knowledge file with a one-line summary per topic. Bootstrapped with `baby_feed_log.md` and `wifi.md` (the existing files).

The agent (a) consults INDEX before guessing knowledge filenames, (b) updates INDEX when creating or substantively reshaping a knowledge file, (c) doesn't touch INDEX for incremental edits within an existing topic.

`agent._load_knowledge_index(base)` reads it with a 4KB soft cap and graceful fallbacks for missing/empty/oversize. Inlined into every prompt as `{knowledge_index}`. Pays off as knowledge files grow past 5 — Claude reaches for the right file in one read instead of scanning blindly.

### 3. Marketing site

`/website/index.html` — single-file, OpenAI-product-page-style design. Warm off-white background, Inter + Newsreader italic, terracotta accent. Sections: hero with two phone chat previews (Telegram + WhatsApp), six feature cards, real Tuesday transcript example, hand-drawn SVG architecture diagram, five-numbered design-choices section with sticky code windows showing household.md + scheduler snippet, black self-host CTA block, footer. Mobile-responsive.

Uses placeholder GitHub URL `github.com/atandon1994/rosey` — needs swapping to real repo when ready.

---

## Open threads (in priority order)

### 1. SIM card + Baileys deployment (Ankit is on this tonight)

He's heading out to buy a SIM card to give Baileys a real WhatsApp account. Last recommendation: **Tello $5/mo via eSIM** (iPhone 13 Pro supports eSIM, instant activation, runs on T-Mobile network, real cellular so WhatsApp accepts verification). Backup: T-Mobile prepaid at Stonestown Galleria for $15/mo if he prefers an in-person setup.

Once SIM is active:
- Install WhatsApp on the iPhone with the new number
- `fly secrets set BAILEYS_MODE=on` (and ensure `BAILEYS_BRIDGE_SECRET` is set)
- Redeploy, watch logs for the Baileys QR code: `fly logs -a rosey`
- Scan QR from his iPhone's WhatsApp → Linked Devices
- Add the bot to the family WhatsApp group
- Smoke-test: mention Rosey in the group, confirm she replies in-thread

### 2. "Forget X" robustness (proposed, awaiting confirmation to ship)

Discussed with him. Current state: fact *corrections* via natural language work cleanly (str_replace handles it). *Deletions* are fuzzy — system prompt doesn't specify the pattern, INDEX.md doesn't auto-update on file delete, there's no undo, and "cancel pending reminder" is conflated with "I did it" (both use the `(acked …)` annotation, which lies about cancellations).

Four improvements proposed:

1. System prompt: "use str_replace to remove single facts; if removing the last entry in a knowledge file, delete the file AND update INDEX.md."
2. System prompt: "confirm before deleting a whole knowledge file or removing a household member."
3. New annotation pattern: `(cancelled by Name at T)` for pending-reminder cancellations, distinct from `(acked …)`. One regex in `reminder_format.py`, one self-skip clause in `scheduler.py`. Honesty in the audit trail.
4. Soft-delete in `FileMemoryTool.delete()` — move files to `/memories/.archive/<path>-<timestamp>` instead of unlinking. ~30 lines.

He said "let's do it" implicitly is yet to come — he hadn't confirmed. Wait for go-ahead before shipping.

### 3. Permanent WhatsApp System User token

Currently using temporary 24-hour Cloud API tokens. Needs to set up a System User in Meta Business Manager and provision a permanent token. Not blocking but the temp tokens will keep expiring.

### 4. Alexa simulator unreachable

`INVALID_RESPONSE` from the simulator with no POST to `/alexa` in Fly logs. Suspected TLS/endpoint config issue. Punted. Low priority — Alexa is the third channel, and Telegram + WhatsApp cover the daily use case.

### 5. Cowork image-attachment debug

In Ankit's memory file: a note that the Cowork app has flaky image transmission. Not Rosey-related; separate ticket. Don't pursue unless he asks.

### 6. Pytest extraction

Smoke tests live in `scripts/test_*.py` files (run as scripts, not via pytest). Worth converting to proper pytest suite eventually, but the current scripts work fine and CI isn't set up yet, so low priority.

---

## Key design decisions worth preserving

These are choices that took thought and have implications a fresh reader wouldn't see from the code.

**Annotation-based state in markdown, not a side-table.** Reminders accumulate state (`(fired …)`, `(escalated …)`, `(acked …)`, `(missed …)`) in the line itself. Crash recovery is free, audit trail is the file you'd read anyway, no schema migration. The whole self-skip pattern (escalate reads the line, sees `(acked`, noops) falls out of this naturally.

**Per-addressee ladders, not per-reminder.** When `@Ankit @Sunanda` are both addressees, each gets their own fire + escalate to their own channels. Without this, the escalation tier would go to a single chosen chat, missing the addressee who set the reminder elsewhere. Ack still cancels everything (line-level annotation, see above).

**Baileys for WhatsApp groups, despite ban risk.** Meta's Cloud API can't reach group chats without Official Business Account (blue tick) status, which is reserved for well-known brands and unreachable for households. Baileys speaks the MultiDevice protocol directly and works today. Ban risk is contained to the bot's WhatsApp account, not family members'. Telegram remains the reliable backbone.

**File-per-purpose memory split.** `household.md`, `reminders.md`, `knowledge/<topic>.md`, `threads/<id>.md`, `groceries/list.md`. Not one giant `memory.md`. Matches Anthropic's published guidance. Smaller files = faster reads = lower per-turn cost.

**Privacy default: deny.** System prompt explicitly says `/memories` is private working storage. One exception (just added): `household.md` is shareable to authorized members on request. Every other file remains private.

**`urg:` tag instead of always-explicit `esc:`/`miss:`.** Higher level abstraction. The agent picks intent (urgent / normal / low-priority); the scheduler picks intervals. Tags can still override per-line.

**Inline a memory-tree snapshot into every prompt.** `_build_memory_index()` produces a sparse hint of what files exist, included in the system prompt as `{memory_index}`. Without this, Claude has to call `view /memories` every turn just to remember what's there. With it, the directory listing is free.

**Loop max 4 iterations.** `MAX_TOOL_ITERATIONS = 4` in `agent.py`. Down from 8 originally. Cuts worst-case latency and ITPM blast radius when the agent thrashes. Empirically enough for the typical household task (view-or-skip → read-target → write-update → reply).

**System prompt is cached.** `cache_control: ephemeral` on the system block. Iterations N≥2 within a turn read from cache at ~10% of input cost. Same-minute calls across messages also hit the cache when `now_local` matches.

---

## Concrete next actions, ranked

1. **Tonight, once Ankit's SIM activates:** walk him through Baileys end-to-end deployment (QR pairing → group add → smoke test).
2. **Ship the "forget X" robustness improvements** if he greenlights — system prompt edits + `(cancelled …)` annotation + soft-delete subclass. ~30 minutes of work.
3. **Set up the permanent WhatsApp System User token** so the Cloud API path doesn't keep expiring.
4. **Replace placeholder GitHub URL in `/website/index.html`** when he pushes the real repo.
5. **Consolidation pass for `reminders.md`** — eventually Fired/Missed/Failed sections will hit the 100KB cap. Build a periodic job that moves entries older than 30 days to `reminders/archive-YYYY-MM.md`.
6. **Convert smoke-test scripts to pytest** when CI is in scope.
7. **Diagnose Alexa simulator** when there's bandwidth.

---

## What to NOT do without explicit go-ahead

- Don't refactor working code without a reason he's signed off on.
- Don't add channels (Signal, SMS, etc.) speculatively — wait for him to ask.
- Don't change the storage format (annotation-based markdown). It works and the audit-trail property is load-bearing.
- Don't enable Baileys in production before he's paired a real phone.
- Don't suggest moving to a database / SaaS / hosted memory layer. The self-hosted-on-your-own-Fly story is core to the product.

---

## Reference: the system prompt (where it lives)

`agent.py` → `SYSTEM_PROMPT` constant. Long (~12KB after templating). Contains:

- Identity framing
- Date/time + CRITICAL date rules for reminders
- Sender identification rules
- Memory layout description with `{memory_index}` and `{knowledge_index}` placeholders
- Tools available
- Per-message procedure
- Privacy rules (with household.md exception)
- Got-it/done flows, ack patterns, casual-ack shortcut, snooze pattern, `{recent_fires_block}` placeholder
- Reminder format docs with urgency rule
- Reply guidance

When you edit it, verify it still `.format()`s correctly with all keys: `from_phone`, `origin_chat`, `now_local`, `tz_name`, `reminder_format`, `memory_index`, `knowledge_index`, `recent_fires_block`.

`SYSTEM_TASK_PROMPT` is the parallel prompt for scheduled invocations (no human user). Smaller. Only takes `now_local`, `tz_name`, `memory_index`.

---

## Quick orientation commands

```bash
# Where are we
cd /Users/ankitpersonal/Documents/rosey

# Run the escalation tests
PYTHONPATH=. python3 scripts/test_escalation_ladder.py

# Read the system prompt
grep -n 'SYSTEM_PROMPT = ' agent.py    # find the start
sed -n '/SYSTEM_PROMPT = """/,/^"""/p' agent.py

# Fly basics
fly status -a rosey
fly logs -a rosey
fly ssh console -a rosey

# Live memory state
ls -la memories/
cat memories/household.md
cat memories/knowledge/INDEX.md
```

---

That's the handoff. Read the first message from the user, ask one clarifying question only if truly necessary, and proceed.
