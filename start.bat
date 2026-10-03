@echo off
rem One-click start on Windows: installs Python packages, builds the console once, starts the server.
cd /d "%~dp0"
python -m pip install -q -r requirements.txt
if not exist console\dist\index.html (
  echo Building the operator console...
  pushd console
  call npm install --no-audit --no-fund
  call npm run build
  popd
)
start "" http://127.0.0.1:8000
python -m tower.server
