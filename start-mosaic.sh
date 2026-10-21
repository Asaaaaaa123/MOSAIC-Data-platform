#!/usr/bin/env sh
# Mosaic launcher (macOS / Linux): sets up on first run, then starts the web app.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "[Mosaic] First run: creating the Python environment..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[dev]"
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[Mosaic] Created .env - add ANTHROPIC_API_KEY and your database keys, then run this again."
  exit 0
fi

PORT=$(grep -E '^BIOPHA_API_PORT=' .env | cut -d= -f2)
PORT=${PORT:-8000}
echo "[Mosaic] Starting on http://127.0.0.1:$PORT (Ctrl+C to stop)"
( sleep 4; (command -v open >/dev/null && open "http://127.0.0.1:$PORT") || (command -v xdg-open >/dev/null && xdg-open "http://127.0.0.1:$PORT") || true ) &
exec .venv/bin/python -m mosaic serve --port "$PORT"
