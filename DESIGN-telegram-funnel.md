# Telegram QR funnel — design

Working doc for the QR → /start → onboarding dialog → group creation flow. Stripe is out of scope (free for the foreseeable future). Multi-tenancy architecture (per-VM vs shared-instance) is **deferred** — this design is written to work either way; we call out the spots where the two diverge.

## Current state

What's in the repo today, and what isn't:

| Piece | Status |
|---|---|
| `rosey-router` Flask app (Telegram webhook, FSM, provisioning) | Code exists in `router/`; deployment unverified |
| `rosey-template` image source on Fly | Setup documented in `router/README.md`; existence unverified |
| Per-household Fly VM provisioning via `flyctl` | Coded, `ROUTER_DRY_RUN` defaults to 1 |
| Telegram-native onboarding FSM | Exists — but only 2 states (`AWAITING_NAME_OR_CODE`, `PROVISIONING`). Asks only for a name. |
| Invite codes (`ROSEY-XXXX`, 7-day TTL, single-use) | Coded and working |
| `/invite <name>` command, `/feedback` forwarding | Coded |
| Soft cap of 25 free households | Coded (`SOFT_CAP` in `telegram_onboarding.py`) |
| `@RoseyHouseholdBot` registered with BotFather | **Not done** — landing page comment confirms placeholder |
| Telegram webhook pointed at `rosey-router.fly.dev/telegram` | **Unverified** |
| Multi-step dialog (household name, members, tz, context, email) | Not built |
| Inline "Create family group" button + deep link | Not built |
| `new_chat_members` event handler (link group chat_id to household) | Not built |
| Pre-rostered members + auto-generated invite codes in welcome | Not built |
| Timezone collection (`SCHEDULER_TZ` is hardcoded to `America/Los_Angeles`) | Not built |

What this doc designs: everything in the bottom block of that table.

## Desired user flow

```
1. Sees QR + "download Telegram and scan me" on rosey.house
2. Scans QR → opens t.me/RoseyHouseholdBot?start=signup in Telegram
3. Chat with @RoseyHouseholdBot opens; taps Start
4. Bot DMs onboarding dialog (6 turns):
   Q1.  Household name? ("Tandons", "The Smiths", "Apartment 4B" — free text)
   Q2.  Who's in the family? — one per line: "Name @telegram_username"
   Q3.  Timezone? — free text ("Pacific", "NYC", "Asia/Kolkata", "UTC-5")
   Q4.  Anything to know upfront? — optional ("we have a dog, kids in school")
   Q5.  Email for the welcome guide? — optional
   (6.  Skipped — payment would go here)
5. Bot replies: "Setting up your household... 🛠️" (provisioning kicks off)
6. Bot replies: "🎉 You're ready! Here are codes to share with family: ..."
   + inline button "Create family group" → t.me/RoseyHouseholdBot?startgroup=ready
7. User taps button → Telegram's add-to-group picker → New Group → names it,
   adds members, hits Create
8. Bot receives new_chat_members event → links group's chat_id to household
   → posts welcome in the group:
   "Hi everyone — I'm Rosey. Anyone here can text me to add to the list,
    set a reminder, ask what's on the calendar..."
```

## FSM design

Extend the existing `onboarding_sessions` table — no schema change needed, the `data` column is already JSON. Add new state values; the existing handler dispatches on `state` so adding states is mechanical.

```
NEW (no row)
  │
  │ /start signup  (payload triggers the new flow;
  │                  /start alone keeps the existing 2-state flow as a fallback)
  ▼
AWAITING_HOUSEHOLD_NAME
  │ free-text name (1–80 chars)
  │ data: {household_name}
  ▼
AWAITING_MEMBERS
  │ one-per-line "Name @username" (or just "Name", or empty/"skip")
  │ data: {household_name, members:[{name, tg_username?}]}
  ▼
AWAITING_TIMEZONE
  │ free-text; parsed to IANA tz (see Timezone section)
  │ data: {..., timezone: "America/Los_Angeles"}
  ▼
AWAITING_CONTEXT_OPT
  │ free-text or "skip"; capped at ~500 chars
  │ data: {..., upfront_context: "..."}
  ▼
AWAITING_EMAIL_OPT
  │ email regex or "skip"
  │ data: {..., email: "..."}
  ▼
PROVISIONING                ← bot says "Setting up your household..."
  │ (background thread; ~30s for per-VM, ~instant for shared-instance)
  │ on success: persist household + members + invite codes to router DB
  │ delete onboarding_sessions row
  ▼
AWAITING_GROUP_CREATE        ← NEW state. Persists in router DB on `households`
  │ (member row exists, household active, but no `group_chat_id` linked yet)
  │ bot sends "🎉 Ready!" + inline button + invite codes
  │
  │ — user taps button — Telegram picker — creates group with bot
  │ — bot receives `new_chat_members` event in the new group
  │ — router looks up inviter (message.from.id) → finds household
  │ — writes group_chat_id to household row
  │ — posts welcome message in group
  ▼
ACTIVE  (households.status = 'active', households.group_chat_id IS NOT NULL)
```

### Why a `/start signup` payload at all

Today the router treats `/start` and anything from an unknown sender as the same trigger for the 2-state FSM. We want the new 6-state flow to be opt-in (the QR signals "this is a fresh signup, walk me through it"), so the existing simpler flow stays available for testers and direct `/start`s.

In `router/app.py:_telegram_extract`, parse `/start <payload>` and pass the payload through to the FSM. The FSM picks `AWAITING_HOUSEHOLD_NAME` if `payload == "signup"`, else falls through to the existing `AWAITING_NAME_OR_CODE` path.

### Why provision BEFORE the "Create group" step

Provisioning takes ~30s on per-VM. We want the household to exist by the time the user creates the group so the bot's first action in the group (welcome message) doesn't require a cold-start delay. Showing "Setting up..." between Q5 and the button is also a natural pause that matches user expectation.

If we end up on shared-instance, "provisioning" is just a few directory mkdirs and the wait disappears — flow is identical, just faster.

## Members + invite codes

User answer to Q2 is a free-text list like:

```
Sarah @sarah_t
Mom @lakshmi_tandon
Dad
Ravi  @ravi_kid_07
```

Parser: split on newlines, strip leading bullets/dashes, split each line on whitespace. First token sequence is the name; any token starting with `@` is the username.

Each parsed entry becomes:

1. **A pre-rostered member** in the router DB. The existing `members` table has `phone` (mis-named — it stores any identifier) and `name`. For a pre-roster entry, we don't have a `chat_id` yet, so we key the row on `pending:<username>` (or `pending:<random>` if no username). The household's `household.md` includes the name so rosey can address them; the identifier is updated to `tg:<chat_id>` on first contact.

2. **An auto-generated invite code** in `invite_codes`. The admin gets all codes in the welcome message:
   ```
   🎉 Set up! Here are codes to share with family:
   
   Sarah → ROSEY-A1B2
   Mom → ROSEY-C3D4
   Dad → ROSEY-E5F6
   Ravi → ROSEY-G7H8
   
   They each open @RoseyHouseholdBot and paste their code.
   ```

### Upgrade-on-first-contact

When the router receives an inbound message from an unknown `tg:<chat_id>` whose Telegram username matches a `pending:<username>` member, treat them as that pre-rostered member instead of starting a new household. Tighten the FSM: if a pending member already redeems the code, both paths converge on the same `members` row — no duplicate.

This needs a new DB lookup function `lookup_pending_by_username(engine, username)` and a small reorder in `app.py` so the username check runs before the FSM kicks in.

## Group creation handoff — the tricky bit

When the user taps "Create family group," Telegram opens the add-to-group picker with the bot pre-selected. They create the group, Telegram sends a `message.new_chat_members` update to the bot's webhook. **Telegram does NOT pass the `startgroup` payload to the bot in that event** — the payload only persists in the inviter's client state.

So we link the group → household via the inviter, not the payload:

```python
# In router/app.py, new branch:
new_members = update["message"].get("new_chat_members", [])
if any(m.get("is_bot") and m["username"] == BOT_USERNAME for m in new_members):
    inviter_id = f"tg:{update['message']['from']['id']}"
    inviter = db.lookup_household(engine, inviter_id)
    if not inviter:
        # Someone added the bot to a random group without going through onboarding.
        # Politely leave or post "you'll need to onboard first" then leave.
        return _refuse_unknown_group(update)
    
    group_chat_id = update["message"]["chat"]["id"]
    if inviter["group_chat_id"] is not None and inviter["group_chat_id"] != group_chat_id:
        # User already has a group linked. New group → either replace, refuse, or
        # support multiple groups. v1: refuse politely, ask them to /switchgroup.
        return _refuse_already_linked(update, inviter)
    
    db.set_group_chat_id(engine, inviter["household_id"], group_chat_id)
    _post_to_household(inviter["fly_app_name"], "/admin/link-group",
                       {"group_chat_id": group_chat_id})
    _send_telegram_message(group_chat_id, WELCOME_GROUP_MESSAGE)
```

### Schema delta

Add a `group_chat_id` column to `households`:

```sql
ALTER TABLE households ADD COLUMN group_chat_id TEXT;
CREATE INDEX IF NOT EXISTS idx_households_group ON households(group_chat_id);
```

The index matters: every inbound group message will hit the router, and the router has to route by `chat_id` (negative for groups, positive for DMs) → household. Without the index that's a table scan per message.

### Routing inbound group messages

Today the router routes by `tg:<chat_id>` of the sender. For group messages, we need to route by the group's `chat_id` instead — every member in the group shares the same group `chat_id`, and the household VM cares about who said what (Telegram `from.id`) but the routing key is the chat.

```python
chat_id = msg["chat"]["id"]
if chat_id < 0:   # group
    household = db.lookup_household_by_group(engine, chat_id)
else:             # DM
    household = db.lookup_household(engine, f"tg:{chat_id}")
```

Both branches end with the same forward call. The household VM's gate logic (`gate.py`) handles "should I respond?" for group messages — that's untouched by this change.

## Timezone

Q3 asks "What timezone are you in?" — most users will answer with a city, an offset, or an abbreviation. None of these are reliably parseable, so we tier the resolver:

1. **IANA name passthrough** (`America/Los_Angeles`, `Asia/Kolkata`) → use as-is if `zoneinfo.ZoneInfo(text)` succeeds.
2. **Common abbreviations** mapped to IANA: `PT/PST/PDT → America/Los_Angeles`, `ET/EST/EDT → America/New_York`, `CT/MT`, `BST/GMT → Europe/London`, `IST → Asia/Kolkata`, `JST → Asia/Tokyo`, ~20 entries.
3. **City fuzzy match** via a bundled table (~200 major cities → IANA). "NYC" → `America/New_York`, "Bangalore" → `Asia/Kolkata`.
4. **UTC offset** (`UTC-5`, `+05:30`) → map to a representative IANA zone (`Etc/GMT+5` is technically valid but treats DST weirdly; better to ask for a city if we got an offset).
5. **Fallback**: ask Claude to parse. Cheap, robust, but adds latency. Worth it for the edge cases.

The resolved IANA name is written into the per-household secret `SCHEDULER_TZ` (currently set globally in `household_template.fly.toml`). For per-VM, that means injecting it into the `fly secrets set` call in `provisioning.py`. For shared-instance, it lives in the household's memory dir (e.g. `household.toml` rendered with the right TZ).

## Email

Q5 is optional. When given, we either:

(a) **Store in router DB** in a new `email` column on `members` (admin only), and trigger nothing immediately. Pull periodically to push into Mailchimp.

(b) **Push to Mailchimp directly** via their API at signup completion. Adds an external dependency in the critical path of provisioning.

(a) is safer for launch — Mailchimp downtime can't block onboarding. We can run a cron later to sync.

Schema delta:

```sql
ALTER TABLE members ADD COLUMN email TEXT;
```

## Architecture-agnostic notes

This design works for both per-VM and shared-instance. The differences:

| Aspect | Per-VM (today) | Shared-instance (hypothetical) |
|---|---|---|
| Provisioning time | ~30s flyctl | ~50ms `mkdir -p` |
| Where memory lives | `/data/memories/` on the household's volume | `/data/households/<household_id>/memories/` on the shared volume |
| How `MEMORY_ROOT` is resolved | Env var, single value per VM | Computed per request from `household_id` |
| Group routing destination | `http://rosey-h-XXXX.internal:8080/telegram` (router → VM) | In-process function call |
| Failure isolation | Hard (VM boundary) | Soft (process boundary, depends on bug class) |
| Cost per household | ~$2/mo always-on (or near-zero with auto-suspend) | Marginal — shares one VM |
| When to switch | If router cost outpaces revenue; ~50+ households is the rough crossover | n/a |

The onboarding FSM, the inviter-keyed group linkage, timezone collection, member pre-rostering, and the invite-code flow are all identical between architectures. The only piece of code that meaningfully differs is `provisioning.py` (today: shells out to flyctl; alt: just touches directories + maybe writes a row).

## Pre-launch readiness checklist

Concrete items, in priority order:

### Must-have (blocks launch)

- [ ] Register `@RoseyHouseholdBot` via BotFather. Save the production token as `TELEGRAM_BOT_TOKEN` and the username as `TELEGRAM_BOT_USERNAME` in `rosey-router`'s Fly secrets.
- [ ] In BotFather, set `/setprivacy` to **Disable** for `@RoseyHouseholdBot`. Without this, the bot won't see most group messages and group rosey appears broken. (gate.py is the firewall — see Decisions #4.)
- [ ] Confirm `rosey-router` Fly app exists, has volume mounted, is reachable at `https://rosey-router.fly.dev/health`. If not: run the one-time setup from `router/README.md`.
- [ ] Confirm `rosey-template` app exists in Fly registry with the latest rosey image pushed. If not: `fly deploy -a rosey-template` once, then `fly machines stop -a rosey-template`.
- [ ] Set Telegram webhook: `curl -X POST "https://api.telegram.org/bot$TOKEN/setWebhook" -d url=https://rosey-router.fly.dev/telegram -d secret_token=$SECRET`
- [ ] Set `ROUTER_DRY_RUN=0` in router secrets so provisioning actually runs.
- [ ] End-to-end smoke test: scan QR from a test phone, run the entire flow (today's 2-state version, since the 6-state isn't built yet), confirm a household VM gets created and the bot replies. **Do this with a Telegram account that is NOT yours** to verify the unknown-sender path.

### Soft-block (launch with broken UX)

- [ ] Ship the 6-state FSM. (This is the bulk of the new work — see Build order below.)
- [ ] Ship the `new_chat_members` handler so groups actually get linked.
- [ ] Ship the timezone resolver so households outside Pacific get correct reminders.
- [ ] Confirm `SOFT_CAP=25` is the right number. With per-VM at ~$2/mo each, 25 = $50/mo even if no one uses it. With auto-suspend that drops to near-zero.

### Nice-to-have

- [ ] Auto-suspend idle household VMs. **NOT just flipping `auto_stop_machines = "stop"`** — see Open Questions #1. Requires moving the scheduler trigger or pre-wake logic to the router. Real project.
- [ ] Add a `/leave` command for self-offboarding (currently you have to flyctl-delete + DB-delete manually).
- [ ] Add nightly backup of `/data/router.db` to S3.

## Decisions

Resolved with Ankit on 2026-05-16:

1. **Existing 2-state FSM coexists.** `/start signup` (the QR payload) drives the new 6-state flow. `/start` alone, or any unknown DM, falls into the existing 2-state flow (name-or-invite-code). Implementation: `app.py:_telegram_extract` parses `/start <payload>`, passes the payload string to the FSM, and the FSM picks the branch.

2. **Auto-resume mid-flow.** Onboarding state persists in `onboarding_sessions` already. Whatever question the user was last on is re-asked when they come back. `/restart` is not a launch requirement; add later if users actually get stuck.

3. **Re-add to group.** If the bot is removed and re-added (or moved to a different group), overwrite `households.group_chat_id` with the new chat_id and post a "🔄 linked to this group" message. v1 doesn't support multiple linked groups per household.

4. **Bot privacy mode: OFF.** Set via BotFather (`/setprivacy` → Disable). `gate.py` is the firewall — it decides which group messages warrant a Claude call. Privacy OFF means the bot sees every group message, but the gate filters to maybe 5–10% of them.

5. **Pre-rostered members without usernames** are still rostered by name. They join via invite code, not username match. The welcome message tells the admin: "For family without Telegram usernames, send them the code so they can join."

## Open questions

1. **Auto-suspend household VMs to reduce Fly cost.** Wanted: stop a household VM when it's been silent for ~48 hours, wake on inbound. Problem: each VM runs APScheduler internally, and a stopped VM doesn't fire timers. Naive auto-stop means missed reminders. Doing this properly requires either (a) moving the scheduler trigger to the router (router holds upcoming-fire times, POSTs to VM at fire time → wakes it), or (b) pre-waking VMs ~30s before any scheduled fire. Either is a multi-day project, not an afternoon. **Decision for launch: keep `auto_stop_machines = "off"` (always-on, ~$2/mo per household, ceiling $50/mo at the 25-cap). Revisit post-launch as a real project.**

## Build order

If we're shipping in a week of evenings (not the 2-3 weeks the handoff projected), here's how I'd sequence the work:

```
Day 0 (today)  — register bot, deploy router if not deployed, smoke-test
                  existing 2-state flow end-to-end. Address must-have checklist.
Day 1          — Extended FSM: 6 states, /start payload parsing, data blob,
                  bot copy for each question.
Day 2          — Member parser, pre-roster DB writes, auto-invite-code
                  generation, welcome message with codes.
Day 3          — Timezone resolver (tiered: IANA → abbr → city → Claude fallback),
                  injection into provisioning's secrets call.
Day 4          — new_chat_members handler, group_chat_id column, inviter-keyed
                  linkage, group routing change. Group welcome message.
Day 5          — Pending-member upgrade on first contact (username match).
                  Email column, basic storage (no Mailchimp push yet).
Day 6          — Integration testing with the friends-and-family beta cohort.
                  Mailchimp sync cron. Whatever else broke.
```

Three weeks down to one because most of the heavy lifting (router, FSM scaffolding, provisioning, invite codes) is already done — we're extending, not building from scratch.

## Risks

The headline risks if we ship this fast:

- **Soft cap is the only billing gate, and auto-suspend isn't a 30-minute fix.** With Stripe out, 25 free households at $2/mo each is $50/mo of Fly burn even if no one engages. Auto-suspend would drop this to near-zero but requires moving the scheduler trigger out of the VM (see Open Questions #1) — not safe to do pre-launch. Live with $50/mo ceiling for now.
- **`new_chat_members` event handling is new code on the critical path.** If it has a bug, every newly-created family group fails to onboard silently. Smoke-test this with at least two real Telegram accounts (Sunanda + you, or you + a burner).
- **Username case sensitivity.** Telegram usernames are case-insensitive but stored as-typed. Always lowercase before DB writes and comparisons.
- **Bot privacy mode default is ON.** If we forget to set OFF in BotFather, the bot won't see most group messages and rosey will appear broken in groups. Step is in the must-have checklist.
- **The handoff's "multi-tenant memory partitioning" task does not need to be done.** Per-VM already isolates. If we later switch to shared-instance, that's a separate project — don't bundle it into this one.
