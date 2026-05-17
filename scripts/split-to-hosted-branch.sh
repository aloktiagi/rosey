#!/usr/bin/env bash
# split-to-hosted-branch.sh
#
# Create a `hosted` branch containing only the SaaS router code, and
# remove that code from the current branch. Both branches will exist in
# parallel with disjoint file trees in one repo.
#
# Pre-conditions:
#   - Working tree is clean (`git status --short` is empty)
#   - You're on the branch you want to keep as the self-host product
#     (typically `main`)
#   - No existing `hosted` branch
#
# What this does:
#   1. Create branch `hosted` from current HEAD
#   2. On `hosted`: remove everything except router/, DESIGN-telegram-funnel.md,
#      HANDOFF.md, .gitignore. Write a new top-level README. Commit.
#   3. Switch back to the original branch
#   4. Remove router/, DESIGN-telegram-funnel.md, HANDOFF.md. Commit.
#
# Does NOT push. Review both branches, then push yourself.

set -euo pipefail

ROSEY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROSEY_DIR"

# --- Sanity ---------------------------------------------------------------

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "Not a git repo: $ROSEY_DIR" >&2
  exit 1
fi
if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree is dirty. Commit or stash changes first." >&2
  echo
  git status --short
  exit 1
fi
if git show-ref --verify --quiet refs/heads/hosted; then
  echo "Branch 'hosted' already exists. Delete it first (git branch -D hosted) if you want to restart." >&2
  exit 1
fi
if [ ! -d "router" ]; then
  echo "router/ doesn't exist on this branch — nothing to extract." >&2
  exit 1
fi

ORIGINAL_BRANCH=$(git rev-parse --abbrev-ref HEAD)
echo "From branch: $ORIGINAL_BRANCH"
echo

# --- Step 1: Create the hosted branch ------------------------------------

echo "→ git checkout -b hosted"
git checkout -b hosted

# --- Step 2: On hosted, keep only the SaaS subset ------------------------

KEEP=(router DESIGN-telegram-funnel.md HANDOFF.md .gitignore)

# Build list of top-level paths to remove (everything that isn't in KEEP).
TO_REMOVE=()
for path in $(git ls-files | awk -F/ '{print $1}' | sort -u); do
  keep_this=false
  for k in "${KEEP[@]}"; do
    if [ "$path" = "$k" ]; then keep_this=true; break; fi
  done
  if ! $keep_this; then TO_REMOVE+=("$path"); fi
done

if [ ${#TO_REMOVE[@]} -gt 0 ]; then
  echo "→ git rm -rq ${TO_REMOVE[*]}"
  git rm -rq "${TO_REMOVE[@]}"
fi

# Write the hosted branch's README at the repo root.
cat > README.md <<'EOF'
# rosey (`hosted` branch)

This is the **`hosted`** branch of the Rosey repo. It contains the SaaS
router code for [rosey.house](https://rosey.house) — the QR-code
onboarding funnel and per-household VM provisioning.

For the open-source **self-host** product (the agent that runs inside
each household VM, and that anyone can deploy on their own Fly account
or laptop), check out the `main` branch.

The two branches share zero source files; they live in one repo for
administrative convenience.

## Architecture

```
[Phones] ── Telegram ──▶ @RoseyHouseholdBot ──▶ POST /telegram
                                                    │
                                                    ▼
                                           [rosey-router Flask app]
                                                    │  chat_id known?
                                          ┌─────────┴─────────┐
                                        YES                   NO
                                          │                   │
                       forward via 6PN to │                   ▼
                       household VM       │          [onboarding FSM]
                                          ▼                   │ 6-step dialog
                              http://rosey-h-XXXX             │ (household name,
                                .internal:8080                │  members, tz, ...)
                                                              ▼
                                                     [provisioning]
                                                     flyctl: app create,
                                                     volume, secrets, deploy
                                                     from rosey-template
                                                     image (~30s)
```

The household VM image is built from `main` and published to
`registry.fly.io/rosey-template:<tag>`. This branch pulls from that
registry — there's no source-tree dependency between the two branches.

## Quick deploy

See `router/README.md` for full setup. Short version:

```bash
git checkout hosted
cd router

fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  TELEGRAM_BOT_TOKEN=... \
  TELEGRAM_BOT_USERNAME=RoseyHouseholdBot \
  TELEGRAM_WEBHOOK_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  ROSEY_INTERNAL_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  ROSEY_OPERATOR_TELEGRAM_ID=<your chat id> \
  FLY_API_TOKEN=<org token> \
  OPENAI_API_KEY=... \
  ROUTER_DRY_RUN=0 \
  --stage -a rosey-router

fly deploy --remote-only -a rosey-router

curl -sS -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d url=https://rosey-router.fly.dev/telegram \
  -d secret_token=$TELEGRAM_WEBHOOK_SECRET
```

Disable bot privacy mode in BotFather (`/setprivacy` → Disable).

## Tests

```bash
cd router && python -m unittest discover -s tests
```

## Contract with `main`

The router serializes a `HOUSEHOLD_TOML` Fly secret that the household
VM (built from `main`) reads at startup:

```toml
household_name = "The Tandons"
shopping_cadence = "weekly"
upfront_context = "we have a dog and 2 kids in school"

[[members]]
name = "Ankit"
telegram_id = "100"            # for known members
notes = ""

[[members]]
name = "Sarah"
telegram_username = "sarah_t"  # for v2 pre-rostered placeholders
notes = ""
```

`main`'s `household.py` accepts `telegram_id`, `telegram_username`, and
a legacy `phone` field. If you add or rename a field here, update
`household.py` on `main` too.
EOF

git add README.md
echo "→ git commit on hosted"
git commit -q -m "hosted: extract SaaS router from main (initial branch state)"

# --- Step 3: Back to main, remove the SaaS code --------------------------

echo
echo "→ git checkout $ORIGINAL_BRANCH"
git checkout -q "$ORIGINAL_BRANCH"

echo "→ git rm -r router/ DESIGN-telegram-funnel.md HANDOFF.md"
git rm -rq router
[ -f DESIGN-telegram-funnel.md ] && git rm -q DESIGN-telegram-funnel.md
[ -f HANDOFF.md ] && git rm -q HANDOFF.md

echo "→ git commit on $ORIGINAL_BRANCH"
git commit -q -m "Extract SaaS router to 'hosted' branch

The router/ directory and hosted-version design docs now live on the
'hosted' branch. This branch ('$ORIGINAL_BRANCH') is the clean
open-source self-host product. The two branches have disjoint file
trees but coordinate via the HOUSEHOLD_TOML env-var contract — see the
hosted branch's README for details."

# --- Done ---------------------------------------------------------------

cat <<EOF

Done.

  $ORIGINAL_BRANCH:  $(git rev-parse --short HEAD)  Extract SaaS router to 'hosted' branch
  hosted:            $(git rev-parse --short hosted)  hosted: extract SaaS router from main

Verify:
  git log --oneline -3 $ORIGINAL_BRANCH hosted
  git checkout hosted && ls
  cd router && python -m unittest discover -s tests   # 104 tests should pass

When happy, push both branches:
  git push origin $ORIGINAL_BRANCH hosted

EOF
