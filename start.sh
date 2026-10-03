#!/usr/bin/env bash
# One-command start on Linux / macOS / WSL: installs Python packages, builds the console once, starts the server.
set -e
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt
if [ ! -f console/dist/index.html ]; then
  echo "Building the operator console..."
  (cd console && npm install --no-audit --no-fund && npm run build)
fi
echo "Open http://127.0.0.1:8000"
python3 -m tower.server
