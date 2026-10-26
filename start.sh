#!/usr/bin/env bash
# Start pood-thai locally and open it in the browser.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
# Use the Claude Code subscription login, not an API key.
unset ANTHROPIC_API_KEY

PORT="${POOD_PORT:-8765}"
uv sync --quiet

(
  # $$ becomes the uvicorn PID after exec; stop polling if the server exits (e.g. startup failed).
  until curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null; do
    kill -0 $$ 2>/dev/null || exit 0
    sleep 1
  done
  open "http://127.0.0.1:${PORT}/"
) &

echo "Starting pood-thai on http://127.0.0.1:${PORT}"
echo "(The first run downloads the Whisper model, ~1.5GB, before the page opens.)"
exec uv run uvicorn --factory pood_thai.app:build_app --host 127.0.0.1 --port "${PORT}"
