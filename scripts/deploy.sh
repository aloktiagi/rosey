#!/usr/bin/env bash
# End-to-end deploy for the SaaS router + household fleet.
#
# Steps:
#   1. fly deploy -a $ROUTER_APP                  (router code: webhook, FSM)
#   2. fly deploy -a $TEMPLATE_APP                (build + push household image)
#   3. fly machines stop -a $TEMPLATE_APP         (template is image-only;
#                                                  keep stopped to save cost)
#   4. fly ssh console -a $ROUTER_APP -C "python /app/router/scripts/migrate_all.py"
#                                                 (loop tenant DB, redeploy
#                                                  each household VM from the
#                                                  freshly-pushed image)
#
# Usage:
#   ./scripts/deploy.sh                # all four steps
#   ./scripts/deploy.sh --skip-router  # household fleet only (router unchanged)
#   ./scripts/deploy.sh --router-only  # just the router (no household roll)
#
# Env overrides (defaults shown):
#   ROUTER_APP=rosey-router
#   TEMPLATE_APP=rosey-template

set -euo pipefail

ROUTER_APP="${ROUTER_APP:-rosey-router}"
TEMPLATE_APP="${TEMPLATE_APP:-rosey-template}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

skip_router=0
router_only=0
for arg in "$@"; do
  case "$arg" in
    --skip-router) skip_router=1 ;;
    --router-only) router_only=1 ;;
    -h|--help) sed -n '2,/^$/p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg"; exit 2 ;;
  esac
done

step() { echo; echo "==> $*"; }

if [ "$skip_router" -eq 0 ]; then
  step "1/4  Deploy router → $ROUTER_APP"
  fly deploy --remote-only --yes \
    --config "$REPO_ROOT/router/fly.toml" \
    --dockerfile "$REPO_ROOT/router/Dockerfile" \
    -a "$ROUTER_APP"
else
  step "1/4  (skipped: --skip-router)"
fi

if [ "$router_only" -eq 1 ]; then
  echo
  echo "✓ Router-only deploy complete."
  exit 0
fi

step "2/4  Build + push household image → $TEMPLATE_APP"
fly deploy --remote-only --yes \
  --config "$REPO_ROOT/fly.toml" \
  --dockerfile "$REPO_ROOT/Dockerfile" \
  -a "$TEMPLATE_APP"

step "3/4  Stop $TEMPLATE_APP machine (image stays in registry)"
machine_id=$(fly machines list -a "$TEMPLATE_APP" --json \
  | python3 -c "import json,sys; m=json.load(sys.stdin); print(m[0]['id'] if m else '')")
if [ -n "$machine_id" ]; then
  fly machines stop -a "$TEMPLATE_APP" "$machine_id"
else
  echo "    (no machine found — already stopped or destroyed)"
fi

step "4/4  Roll all household VMs onto the new image"
fly ssh console -a "$ROUTER_APP" -C "python /app/router/scripts/migrate_all.py"

echo
echo "✓ Deploy complete."
