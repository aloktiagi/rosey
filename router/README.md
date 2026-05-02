# Rosey router

Tenant router for the Rosey-as-a-service architecture. One public Telegram
webhook (`POST /telegram`), many households, each with its own dedicated Fly
VM and isolated memory. For each inbound message:

- **Known sender** → forward to that household's per-VM Fly app via 6PN
- **Unknown sender** → hand to the Telegram-native onboarding FSM, eventually
  provision a new household VM, register them in the tenant DB

## Architecture

```
[Phones] ── Telegram ──▶ Telegram bot ──▶ POST /telegram
                                              │
                                              ▼
                                     ┌──────────────────┐
                                     │  rosey-router    │ (this directory)
                                     │  Flask + SQLite  │
                                     │  on Fly volume   │
                                     └────┬─────────────┘
                                          │ chat_id in tenant DB?
                              ┌───────────┴───────────┐
                              │                       │
                            YES                       NO
                              │                       │
              forward via 6PN │                       ▼
              with internal   │              [onboarding FSM]
              token           │                       │
                              ▼                       │ /invite code or
              http://<fly_app_name>.internal          │ first signup; FSM
                   :8080/telegram                     │ kicks off →
                              │                       ▼
                              ▼              [provisioning thread]
                   [household VM runs                 │ flyctl: app create,
                    agent, replies via                │ volume, secrets,
                    Telegram bot API]                 │ deploy from template
                                                      ▼
                                             [new rosey-h-XXXX
                                              app live in ~3 min]
```

## Components

```
router/
├── app.py                       # Flask /telegram + /health
├── telegram_onboarding.py       # Telegram-native FSM
├── provisioning.py              # shells out to flyctl
├── db.py                        # SQLAlchemy queries (households, members,
│                                #   onboarding_sessions, invite_codes)
├── schema.sql                   # tenant DB DDL
├── household_template.fly.toml  # bundled in image; --config when deploying new VMs
├── Dockerfile                   # python:3.11-slim + flyctl install
├── fly.toml                     # the router app's own Fly config
└── scripts/
    ├── migrate_all.py           # admin: redeploy every household VM from latest template image
    └── wipe_tenants.py          # admin: clear tenant DB
```

## The two Fly apps that aren't household VMs

| App | Purpose | Lifecycle |
|---|---|---|
| `rosey-router` | Public Telegram webhook, FSM, provisioning | Always-on, ~$5/mo |
| `rosey-template` | **Image-only registry source.** Hosts the rosey image at `registry.fly.io/rosey-template:<tag>` so new household VMs have something to deploy from. | Machine **stopped** to save cost (~$0.15/mo just for the volume); the image stays in the registry whether the machine runs or not. Must NEVER be destroyed. |

### Why a separate template app exists

When a new household onboards, `provisioning.py` runs `fly deploy --image
registry.fly.io/<TEMPLATE_APP>:<latest-tag> -a <new-app>` instead of building
from source. This:
- Skips the ~3-minute Docker build per signup (deploys in ~30s instead)
- Means the router doesn't need the rosey source tree

But Fly's registry is **per-app** — `registry.fly.io/<app-name>:<tag>` — so
destroying the template app deletes its registry namespace. Use a stable
`rosey-template` app dedicated to that purpose.

To update the household VM image (e.g. ship a bug fix to all NEW households):

```bash
cd <repo-root>
fly deploy --remote-only --config fly.toml --dockerfile Dockerfile -a rosey-template
# new households deploy with the new image; existing households keep their
# pinned digest until they're individually re-deployed (see scripts/migrate_all.py).
```

## Deploy the router

```bash
cd <repo-root>/router

# One-time:
fly launch --no-deploy --copy-config --name rosey-router --region sjc --org personal
fly volumes create router_data --size 1 --region sjc -a rosey-router --yes
fly tokens create org -o personal --expiry 8760h --name rosey-router-provisioning  # save output

fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  OPENAI_API_KEY=sk-... \
  TELEGRAM_BOT_TOKEN=1234567890:AAEh... \
  TELEGRAM_BOT_USERNAME=your_bot_username \
  TELEGRAM_WEBHOOK_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  ROSEY_OPERATOR_TELEGRAM_ID=<your-chat-id-for-feedback-forwarding> \
  ROSEY_INTERNAL_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  FLY_API_TOKEN="<the org token>" \
  ROUTER_DRY_RUN=0 \
  ROSEY_TEMPLATE_APP=rosey-template \
  --stage -a rosey-router

fly deploy --remote-only -a rosey-router

# Then point the Telegram webhook at https://rosey-router.fly.dev/telegram:
curl -sS -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d url=https://rosey-router.fly.dev/telegram \
  -d secret_token=$TELEGRAM_WEBHOOK_SECRET
```

### Critical env / secrets on the router

| Name | Why |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Propagated as secrets onto each new household VM (your shared SaaS keys) |
| `TELEGRAM_BOT_TOKEN` | Propagated to household VMs so they can reply via the Telegram bot API |
| `TELEGRAM_BOT_USERNAME` | Used in welcome messages and onboarding copy |
| `TELEGRAM_WEBHOOK_SECRET` | Validated on every inbound webhook via `X-Telegram-Bot-Api-Secret-Token` |
| `ROSEY_INTERNAL_TOKEN` | Shared HMAC secret. Router sends it on forwards; household VMs trust it for skipping bot-API signature validation |
| `ROSEY_OPERATOR_TELEGRAM_ID` | Chat ID that receives forwarded `/feedback` messages |
| `FLY_API_TOKEN` | Lets the router shell out to `flyctl` from inside its container. Org-scoped; rotate yearly |
| `ROUTER_DRY_RUN` | `1` skips real Fly calls (default for tests); `0` enables real provisioning |
| `ROSEY_TEMPLATE_APP` | App whose registry hosts the household VM image. **Must be `rosey-template`** in production |
| `ROSEY_FLY_ORG`, `ROSEY_FLY_REGION` | Defaults `personal` / `sjc` |
| `ROSEY_HOUSEHOLD_CONFIG` | Path to `household_template.fly.toml` inside the container. Set automatically in `router/fly.toml` |
| `DATABASE_URL` | `sqlite:////data/router.db` in production |

## Run locally

```bash
# from <repo-root>
.venv/bin/python -m router.app                     # Flask on :8081

# To exercise real provisioning locally:
ROUTER_DRY_RUN=0 .venv/bin/python -m router.app
# (must have flyctl installed and authed; consumes Fly + API credits)
```

## Onboarding state machine

```
NEW (no row)             → AWAITING_NAME_OR_CODE  ("/start" → "What's your name? Or paste an invite code.")
AWAITING_NAME_OR_CODE    → (handed off to provisioning, single-member household)   if user typed a name
                         → (added to existing household)                           if user pasted ROSEY-XXXX
PROVISIONING             → (row deleted; new household + member rows committed)
```

State persisted in `onboarding_sessions` keyed by `tg:<chat_id>`.
Invite codes live in the `invite_codes` table (TTL 7 days, single-use).

## Forward path (router → household VM)

```python
# router/app.py
url = f"http://{fly_app_name}.internal:8080/telegram"
headers = {"X-Rosey-Internal-Token": ROSEY_INTERNAL_TOKEN}
requests.post(url, json={"chat_id": chat_id, "text": text, "name": sender_name},
              headers=headers, timeout=15)
```

The household VM's `app.py` recognizes the header and accepts the request.
Reply goes back to the user via the Telegram bot API directly — the router
doesn't relay it.

For local dev (no 6PN), set `ROSEY_HOUSEHOLD_BASE_URL=http://localhost:8080`
and the router will hit that instead of `<app>.internal:8080`.

## Provisioning a new household

When the FSM hits `PROVISIONING`, `provisioning.kick_off()` spawns a
background thread that:

1. Generates `rosey-h-<8-hex>` app name
2. `fly apps create <name>`
3. `fly volumes create memory_data ...`
4. `fly secrets set` for ANTHROPIC, OPENAI, TELEGRAM_BOT_TOKEN,
   ROSEY_INTERNAL_TOKEN, **HOUSEHOLD_TOML** (rendered TOML with members)
5. `fly deploy --image registry.fly.io/$ROSEY_TEMPLATE_APP:<latest-tag> -a <name> --config household_template.fly.toml`
6. Inserts `households` + `members` rows; deletes `onboarding_sessions` row
7. Sends welcome message via Telegram bot API to admin + each member

On first boot the new VM reads the `HOUSEHOLD_TOML` env var and renders
`/data/memories/household.md`. From that point it behaves identically to any
single-tenant rosey deployment.

## Soft cap

`telegram_onboarding.SOFT_CAP = 25`. Past this, new senders get a "we're at
capacity" reply instead of starting onboarding. Bump in code when ready to
grow.

## Open issues / next sessions

1. **`rosey-template` machine is stopped to save cost** but the app exists.
   If anyone runs `fly deploy` without overriding, it'll restart that machine.
   Document: only redeploy with explicit `-a rosey-template` and immediately
   `fly machines stop` after.
2. **Auto-suspend household VMs.** Currently each new household's VM has
   `auto_stop_machines = "off"` (always on, ~$2/mo each). Switch to `"stop"`
   so they suspend when idle and wake on inbound — drops cost to near-zero
   per inactive household. Edit `household_template.fly.toml`.
3. **Admin/offboard.** No `/leave` flow yet; no way for a household to
   destroy its own app from inside the bot. Manual via flyctl + tenant-DB delete.
4. **Postgres swap.** SQLite is fine for the first ~100 households. Swap to
   Neon when concurrent writers become a thing — only `DATABASE_URL` changes.
5. **Backups.** Nightly `tar /data/router.db` to S3, plus per-household
   `/data/memories/` snapshots. Currently relies entirely on Fly's volume
   snapshot retention (5 days).
