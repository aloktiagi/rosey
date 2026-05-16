#!/usr/bin/env bash
# Container entrypoint that launches both processes side-by-side:
#   1. Baileys sidecar (Node.js) on loopback :3001 + outbound forwarder
#      to Python on :8080
#   2. Hypercorn (Python/Quart) on :8080 — public-facing
#
# We send SIGTERM to both children when the container is shutting down,
# wait for either to exit, then exit ourselves with whichever code came
# first. Fly's container-restart will bring everything back fresh.
set -uo pipefail

cleanup() {
  echo "[start] received signal, shutting down children"
  # Be polite first, kill -9 if needed
  [[ -n "${BAILEYS_PID:-}" ]] && kill -TERM "$BAILEYS_PID" 2>/dev/null || true
  [[ -n "${HYPERCORN_PID:-}" ]] && kill -TERM "$HYPERCORN_PID" 2>/dev/null || true
  wait
  exit 0
}
trap cleanup SIGTERM SIGINT

# Skip Baileys entirely if the operator hasn't opted in. The Cloud API
# path still works for 1:1 WhatsApp; Baileys is only needed for groups.
if [[ "${BAILEYS_MODE:-off}" == "on" ]]; then
  echo "[start] launching baileys sidecar"
  ( cd /app/baileys && node index.js ) &
  BAILEYS_PID=$!
  echo "[start] baileys pid=$BAILEYS_PID"
else
  echo "[start] BAILEYS_MODE=off — skipping baileys sidecar (Cloud API only)"
fi

echo "[start] launching hypercorn"
# Bind to [::] (dual-stack) instead of 0.0.0.0 so the router can reach this
# VM over Fly's 6PN private network (IPv6-only). On Linux this also accepts
# IPv4 connections since IPV6_V6ONLY defaults to off.
hypercorn server:asgi_app \
  --bind '[::]:8080' \
  --access-logfile - \
  --error-logfile - &
HYPERCORN_PID=$!
echo "[start] hypercorn pid=$HYPERCORN_PID"

# Wait for either child to exit. Whichever one dies first crashes the
# container — Fly will restart and we get a clean state.
wait -n
EXIT=$?
echo "[start] one child exited with code=$EXIT, tearing down siblings"
cleanup
