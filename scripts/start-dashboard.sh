#!/usr/bin/env bash
# Start AMRP on macOS / Linux: REST API (8000) + control API (8712) + dashboard (8501), all on 127.0.0.1.
# Usage (from the repo root):  ./scripts/start-dashboard.sh   then open http://127.0.0.1:8501
# Press Ctrl+C to stop everything.
set -euo pipefail
API_PORT="${API_PORT:-8000}"; CONTROL_PORT="${CONTROL_PORT:-8712}"; DASHBOARD_PORT="${DASHBOARD_PORT:-8501}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi

# One-time: dashboard dependency
if ! "$PY" -c "import streamlit" 2>/dev/null; then
  echo "Installing dashboard requirements (one time)..."
  "$PY" -m pip install -r dashboard/requirements.txt
fi

# Local-only settings for this session. The control token is random per start and never shown.
export APP_ENV=development LOG_FORMAT=text AUTH_ENABLED=false
export AMRP_CONTROL_TOKEN="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AMRP_API_URL="http://127.0.0.1:$API_PORT" AMRP_CONTROL_URL="http://127.0.0.1:$CONTROL_PORT"

mkdir -p logs
"$PY" -m amrp.dashboard.launch api --port "$API_PORT" >logs/api.out.log 2>logs/api.log &
API_PID=$!
"$PY" -m amrp.dashboard.launch control --port "$CONTROL_PORT" >logs/control.out.log 2>logs/control.log &
CTL_PID=$!
trap 'kill $API_PID $CTL_PID 2>/dev/null || true; echo "Stopped API and control API."' EXIT INT TERM

printf "Waiting for the API..."
for _ in $(seq 1 40); do
  if "$PY" -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$API_PORT/health', timeout=2)" 2>/dev/null; then echo " ready"; break; fi
  sleep 0.5; printf "."
done
echo "API:       http://127.0.0.1:$API_PORT/docs   (logs: logs/api.log)"
echo "Dashboard: http://127.0.0.1:$DASHBOARD_PORT  (Ctrl+C to stop all)"
"$PY" -m streamlit run dashboard/app.py --server.port "$DASHBOARD_PORT" --server.address 127.0.0.1 \
  --server.headless true --browser.gatherUsageStats false
