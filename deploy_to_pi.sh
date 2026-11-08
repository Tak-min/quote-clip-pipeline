#!/usr/bin/env bash
# Mac → Pi: ship the COMMITTED pipeline (git HEAD, card-mode guards intact) plus the KOTONEX
# scheduler files, refresh the app quote data, and run the tests on the Pi.
# Never copies .env, secrets/, accounts.json, output/ or work/.
# Usage: ./deploy_to_pi.sh [user@host]   (default taku8@100.102.144.116)
set -euo pipefail
TARGET="${1:-taku8@100.102.144.116}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="${KOTONEX_DATA:-$HOME/kotodama-codex/data}"
cd "$SRC"
git archive --format=tar HEAD | ssh "$TARGET" 'mkdir -p ~/quote-clip-pipeline && tar -x -C ~/quote-clip-pipeline'
# files not (yet) in HEAD that the Pi schedule needs
EXTRA=(quote_clips/kotonex_daily.py tests/test_kotonex_daily.py deploy_to_pi.sh .gitignore README.md)
tar -cf - "${EXTRA[@]}" | ssh "$TARGET" 'tar -x -C ~/quote-clip-pipeline'
ssh "$TARGET" 'mkdir -p ~/quote-clip-pipeline/data/app ~/quote-clip-pipeline/logs'
scp -q "$DATA"/{quotes_figures.json,quotes_anime.json,works.json,scenes.json} "$TARGET:quote-clip-pipeline/data/app/"
ssh "$TARGET" 'cd ~/quote-clip-pipeline && .venv/bin/python -m pytest -q 2>&1 | tail -2'
