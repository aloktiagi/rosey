# Rosey — handoff (2026-05-15)

Context for the next chat. Written at the close of the pre-launch push.
Launch is tomorrow (2026-05-16).

---

## Status

**Shipping tomorrow:** the open-source self-host product + the marketing
site at rosey.house. Free, MIT, deploy-it-yourself.

**Being teased on the site but not actually built yet:** the hosted
option (Option 2 on the landing page). The "Open in Telegram" button +
QR currently point at `https://t.me/RoseyHouseholdBot?start=signup` —
that bot username has **not been registered yet**. Scanning before the
bot exists will yield Telegram's "user does not exist" error.

**Decision already made (don't re-litigate):** the hosted option will
be **Telegram-first**, not WhatsApp. Reasoning:
- WhatsApp Cloud API supports DMs but cannot do family groups (requires
  Official Business Account / blue tick). Losing groups loses most of
  the product.
- WhatsApp Baileys *can* do groups but requires one phone number per
  family, which means SIM provisioning, which is not cleanly automatable
  without an eSIM-provider integration.
- Telegram sidesteps both: one bot serves all customers, native group
  support, no SIM, no per-message cost. Multi-tenant data isolation
  becomes a directory-partition problem on our side, not a
  per-family-infra problem.

WhatsApp hosted is "coming soon" on the page, but realistically blocked
on either (a) eSIM API integration, or (b) accepting Cloud-API-only and
giving up group support. Punt until there's revenue justifying it.

---

## Uncommitted on disk (this session, not in git yet)

If the next chat runs `git status`, these are the changes sitting in
the working tree:

**Code:**
- `baileys/index.js` — WhatsApp image support: downloads `imageMessage`
  media via `downloadMediaMessage`, base64-encodes (with 4 MB cap),
  forwards `image_b64` + `image_mime` in the payload to Python.
- `whatsapp_handler.py` — `handle_baileys_event` now accepts
  `image_b64` / `image_mime`, passes them through to `handle_message`.
  Image-only messages allowed in DMs; image-only group messages still
  dropped at the gate (no text to classify).
- `agent.py` — system prompt now requires confirmation echoes on every
  shared-state write (grocery list, reminders, events, knowledge,
  household, pantry). Bumped the casual reply-length cap from ~200 to
  ~300 chars with an explicit carve-out for confirmation replies that
  include a list. See the "Reply guidance" + "Confirm what you did"
  blocks in `SYSTEM_PROMPT`.

**Marketing site:**
- `website/index.html` — full restructure:
  - Hero now contains the chat preview animation BETWEEN the lede and
    the signup form, then Step 1 (family type) → Step 2 (email) → Get
    started button → Step 3 (two-path cards: self-host + hosted)
  - Self-host dark card has install-strip + GitHub/Setup buttons
  - Hosted card has a real client-rendered QR (qrcode.js from jsdelivr
    CDN), an "Open in Telegram" button, and bullets covering: 5-min
    onboarding, isolated partition per household, "Telegram only for
    now — WhatsApp coming soon"
  - Removed several lines per user feedback (former eyebrow,
    section ledes, redundant phrasing — see git diff for specifics)
- `website/style.css` — ~140 lines of new CSS under
  `/* ─── get-started: two paths ─── */` for `.paths`, `.path-card`,
  `.path-tag`, `.path-pros`, `.hosted-qr`, `.qr-box`, etc.
- `website/docs/index.html`:
  - Removed the "Three doors" headline
  - Architecture diagram now has brand logos (Telegram paper plane,
    WhatsApp chat bubble, Alexa light ring, Anthropic 6-spoke star)
    inline as SVG glyphs (correct shape + brand color, not official
    rasterized logos)
  - New **Memory section** explaining the filesystem-backed memory
    model with links to Anthropic's memory-tool docs and context-
    engineering essay
- `website/privacy/index.html` — rewritten to 3 short paragraphs
  (self-host scope / hosted scope on SOC 2 Type II Fly / Anthropic +
  chat-platform footnote). Stripped all the original sections.
- `website/about/index.html` — Alok and Ankit names now link to their
  LinkedIn profiles in the credits line.

**None of this is deployed.** The bot in production is still running
the previous version (last deploy was after the recurring-reminders
work + Anthropic 529 overload handling). To deploy: `git add . &&
git commit && fly deploy`.

---

## Next task: build the Telegram QR onboarding funnel

This is the work item for the new chat. Roughly 2–3 weeks of focused
effort for a clean paid-funnel MVP. Can compress to 1–2 weeks if
willing to do some Stripe ops manually for the first cohort.

### Architecture sketch

- One new Telegram bot `@RoseyHouseholdBot` (registered with BotFather,
  separate from any personal/dev bot). Single bot serves every paying
  household.
- One shared Rosey backend on Fly. Multi-tenant: memory directories
  partitioned by `household_id` under
  `/data/households/<household_id>/memories/...`.
- A small SQLite table (`households.db`?) mapping
  `tg:<user_id>` → `household_id`. Same table is the source of truth
  for: who's in which household, billing state, trial expiry, the
  Telegram chat IDs to route to.
- Onboarding state machine in `telegram_bot.py`: detects `/start signup`,
  persists `onboarding_step` per user, drives the dialog turn by turn
  (household name → members → timezone → upfront context → Stripe
  checkout → "Create family group" inline button).
- Stripe integration: Checkout sessions, webhook for
  `checkout.session.completed`, subscription state per household,
  14-day trial tracking, dunning on failed payment, cancellation flow.
- `chats.member_added` event handler: when bot is added to a new group,
  link that `chat_id` to the most recent onboarder's household, post a
  welcome message, set `origin_chat` on `household.md`.

### Subtasks, roughly in build order

1. **Register `@RoseyHouseholdBot` with BotFather.** 5 minutes.
   Save the token as a Fly secret distinct from the dev token.
2. **Multi-tenant memory partitioning** — invasive but unavoidable.
   Touches `agent.py`, `scheduler.py`, `memory_tool.py`,
   `telegram_bot.py`, `whatsapp_handler.py`. Every file path that
   currently hardcodes `/memories` or `MEMORY_ROOT` needs to resolve
   `household_id` first. The bug-here-leaks-data-cross-tenant blast
   radius means this needs careful testing. 2–3 days, plus thorough
   integration tests.
3. **Households SQLite table + resolver function.** `get_household_id(
   identifier: str) -> str | None`. Cached. New rows on onboarding
   completion. ~Half a day.
4. **Onboarding state machine.** Detect `/start signup` payload, drive
   the dialog. The agent prompt already handles confirmation echoes;
   the onboarding flow just sets `onboarding_step` in the user's memory
   and feeds the agent a slightly different system prompt while
   `step < final`. ~Half a day.
5. **Stripe checkout + webhook.** Test mode first. 1–2 days including
   trial logic + cancellation. Use Stripe CLI for local webhook
   forwarding: `stripe listen --forward-to localhost:8080/stripe-webhook`.
6. **`startgroup` handler.** Telegram emits a
   `message.new_chat_members` event when the bot is added to a group.
   Hook that, identify the inviter, link `chat_id` to their household
   in the SQLite table, post a welcome message in the group. Few hours.
7. **Update the landing page QR/button** to the real bot username
   once registered. Two find-and-replaces in `index.html`.

### Testing plan (don't skip — multi-tenant bugs are expensive)

1. **Local dev:** dev bot (`@RoseyHouseholdDevBot`) + long-polling
   backend + Stripe test mode. Chat with the dev bot from your phone.
   Cycle is seconds.
2. **Staging deploy:** separate Fly app, separate bot, Stripe still in
   test mode. Run through the full flow as a customer would.
3. **Friends-and-family beta:** prod bot live, 100% Stripe discount
   coupon. 5–10 real households (Alok's family, Ankit's parents, a
   couple of friends). They go through the real flow without paying.
   Watch what breaks.
4. **Public launch with paid pricing.**

For the Telegram group-creation step: Telegram requires at least one
other human member to create a group. Either recruit Sunanda (or any
real family member) as the test counterparty, or stand up a second
Telegram account on a Google Voice number.

---

## Risky bits / watch list

- **Memory partitioning bug surface.** Any code path that opens a file
  under `/memories/...` is suspect. Easiest belt-and-suspenders: route
  ALL memory access through a resolver that takes a household_id, and
  fail loudly if a household_id can't be determined. The current
  single-tenant `MEMORY_ROOT` env var becomes a per-household path.
- **Stripe test-mode → live-mode flip.** Don't forget to swap secrets
  on prod. Easy to ship live by accident if test mode keys are still
  in Fly secrets.
- **Bot's permissions in groups.** Telegram bots have a "privacy mode"
  setting controlled via BotFather. With privacy mode ON (default), the
  bot only sees messages that mention it or reply to it. The current
  Rosey gate already does this filtering, so privacy mode OFF gives
  more flexibility. But OFF means the bot receives every group message
  — be sure the gate stays correct or the agent burns a turn per
  message. Decide explicitly via BotFather's `/setprivacy`.
- **Trial expiration UX.** The clock starts when? Account creation?
  First successful Stripe checkout? Decide upfront so the "trial
  ending" warning timestamps are unambiguous.
- **Concurrent bot operation.** If `@RoseyHouseholdBot` (prod) and
  `@RoseyHouseholdDevBot` (dev) both run, they're independent — same
  code, different tokens, different SQLite stores. Don't mix.

---

## Open pending tasks

Useful to see what's already on the list before adding new ones:

- #2 — Ship "forget X" robustness bundle
- #3 — Provision permanent WhatsApp System User token
- #5 — Diagnose Alexa simulator INVALID_RESPONSE
- #6 — Add periodic archival job for reminders.md
- #7 — Convert smoke-test scripts to pytest
- #13 — Add social-sharing assets to landing page (OG, Twitter, apple-touch-icon)
- #14 — Mobile responsiveness pass on landing page
- #15 — Final landing page copy + tone pass
- #16 — Paste welcome email into Mailchimp + activate Customer Journey
- #17 — Draft day-3 follow-up survey email
- #18 — Create demo walkthrough video for landing page
- #19 — Distinguish WhatsApp LIDs from phone numbers in handler + roster

The "Telegram QR funnel" work above doesn't have task IDs yet — break
it down into the 7 subtasks and add to the tracker at the start of the
next chat.

---

## Reference

### Key paths

- Repo root: `/Users/ankitpersonal/Documents/rosey`
- Site: `website/index.html`, `website/style.css`,
  `website/{docs,privacy,about}/index.html`
- Agent: `agent.py` (handle_message, SYSTEM_PROMPT, SYSTEM_TASK_PROMPT)
- Scheduler: `scheduler.py` (APScheduler + SQLite, reconcile,
  cross-channel ack broadcast)
- Memory tool: `memory_tool.py` (FileMemoryTool subclass of
  Anthropic's BetaLocalFilesystemMemoryTool)
- Channels: `telegram_bot.py`, `whatsapp_handler.py`, `baileys/index.js`
- Gate: `gate.py` (group-message classifier)
- Reminder format: `reminder_format.py`
- Tools list passed to Claude: `tools.py` → `default_tools(memory)`

### External services

- Fly.io: `fly deploy`, `fly logs`, `fly secrets set KEY=value`,
  `fly ssh console`. SOC 2 Type II compliant.
- Anthropic Claude API: `claude-sonnet-4-6` default;
  `ROSEY_MODEL` env var overrides (used during the 529 overload to
  swap to Opus 4.7).
- Mailchimp: signup form on landing page; audience managed by Ankit;
  MMERGE7 = family type dropdown answer.
- Cloudflare: rosey.family and rosey.house DNS + email routing;
  Cloudflare Pages for site hosting.
- GitHub: `https://github.com/aloktiagi/rosey` — Alok is primary
  maintainer; Ankit's local fork is what gets `fly deploy`'d for the
  personal instance.

### Memory locations (auto-memory across chats)

`/Users/ankitpersonal/Library/Application Support/Claude/local-agent-mode-sessions/.../memory/`

Existing files there cover: Ankit's family, Rosey requirements, Cowork
image-transmission bug. The new chat will load these automatically via
`MEMORY.md` if the conversation is in the same workspace.

---

## TL;DR for the new chat

> "I'm starting the Telegram QR onboarding funnel for Rosey. The
> landing page already has the hosted option teased (Option 2 card +
> QR placeholder pointing at `@RoseyHouseholdBot?start=signup`). The
> bot doesn't exist yet. First thing to do is register the bot with
> BotFather, then build out multi-tenant memory partitioning, then the
> onboarding state machine, then Stripe. See HANDOFF.md in the repo
> root for the full context."
