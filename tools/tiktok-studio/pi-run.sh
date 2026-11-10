#!/usr/bin/env bash
# Pi: keep a headless Chromium (TikTok Studio login lives in its profile) and schedule today's TikTok jobs.
# cron: */30 * * * *  ~/quote-clip-pipeline/tools/tiktok-studio/pi-run.sh >> ~/quote-clip-pipeline/logs/tiktok-studio.log 2>&1
# Login once (or after TikTok logs the profile out): see README "TikTok Studio（Pi）".
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$HOME/.local/node/bin:$PATH" KOTONEX_LOCAL=1 TIKTOK_CDP="http://127.0.0.1:9222"
PROFILE="$HOME/.config/kotonex-tiktok"

if ! curl -fs http://127.0.0.1:9222/json/version >/dev/null; then
  mkdir -p "$PROFILE"
  nohup chromium --headless=new --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
    --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check --window-size=1400,1000 \
    --lang=ja-JP --disable-dev-shm-usage about:blank >/dev/null 2>&1 &
  for _ in $(seq 1 30); do curl -fs http://127.0.0.1:9222/json/version >/dev/null && break; sleep 1; done
fi

[ "${1:-}" = "--browser-only" ] && exit 0
echo "== $(date '+%F %T')"
exec flock -n /tmp/kotonex-tiktok-studio.lock node schedule.js "$@"
