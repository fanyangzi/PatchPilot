#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"

API_PORT="${PATCHPILOT_API_PORT:-8010}"
CONSOLE_PORT="${PATCHPILOT_CONSOLE_PORT:-5173}"
PIDS=()

cleanup() {
  trap - INT TERM EXIT
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup INT TERM EXIT

if [ ! -d "$ROOT/.venv" ]; then
  echo "No .venv found. Run: python -m venv .venv && .venv/bin/pip install -e '.[api]'" >&2
  exit 1
fi

if [ ! -x "$ROOT/.venv/bin/uvicorn" ]; then
  echo "Missing .venv/bin/uvicorn. Install the API dependencies first." >&2
  exit 1
fi

if [ ! -d "$ROOT/apps/console/node_modules" ]; then
  echo "Missing apps/console/node_modules. Run: (cd apps/console && npm install)" >&2
  exit 1
fi

"$ROOT/.venv/bin/uvicorn" patchpilot.api:app \
  --app-dir "$ROOT/src" \
  --host 127.0.0.1 --port "$API_PORT" --reload &
API_PID=$!
PIDS+=("$API_PID")

cd "$ROOT/apps/console"
VITE_DEV_API_TARGET="${VITE_DEV_API_TARGET:-http://127.0.0.1:${API_PORT}}" \
  npm run dev -- --host 127.0.0.1 --port "$CONSOLE_PORT" &
CONSOLE_PID=$!
PIDS+=("$CONSOLE_PID")

for _ in {1..30}; do
  if curl -fsS "http://127.0.0.1:${CONSOLE_PORT}/" >/dev/null 2>&1; then break; fi
  sleep 0.2
done
open "http://127.0.0.1:${CONSOLE_PORT}" 2>/dev/null || xdg-open "http://127.0.0.1:${CONSOLE_PORT}" 2>/dev/null || true

wait "$API_PID" "$CONSOLE_PID"
