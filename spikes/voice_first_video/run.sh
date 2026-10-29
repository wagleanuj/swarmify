#!/usr/bin/env bash
# PROTOTYPE: voice takes, edit, render. Needs the Chatterbox venv at /tmp/tts-bench/cb.
set -euo pipefail
cd "$(dirname "$0")"
source /tmp/tts-bench/cb/bin/activate
PYTORCH_ENABLE_MPS_FALLBACK=1 python voice.py video/public
python edit.py video/public
cd video && npm ci --silent && npm install --silent @remotion/captions@4.0.538
npx remotion render src/index.ts Main out/hedgehog.mp4 --port=3417
