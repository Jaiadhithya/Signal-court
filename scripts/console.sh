#!/usr/bin/env bash
# Git Bash convenience entry point. Windows uses the same hidden server as the double-click launcher.
set -euo pipefail
console_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$console_root"
if [[ -f .venv/Scripts/activate ]]; then
  source .venv/Scripts/activate
  exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$console_root/scripts/console.ps1")"
fi
if [[ ! -f .venv/bin/activate ]]; then
  echo 'Missing .venv. Run uv sync once during setup.' >&2
  exit 1
fi
source .venv/bin/activate
mkdir -p workspace/console
python - <<'PY'
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

url = 'http://127.0.0.1:8765'
with socket.socket() as probe:
    running = probe.connect_ex(('127.0.0.1', 8765)) == 0
if not running:
    with Path('workspace/console/server.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'signalcourt.web'], stdout=log, stderr=log,
                                   start_new_session=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + '/api/status', timeout=2):
                break
        except OSError:
            if process.poll() is not None:
                raise SystemExit('The console stopped. See workspace/console/server.log.')
            time.sleep(.3)
    else:
        raise SystemExit('The console did not answer within two minutes. See workspace/console/server.log.')
webbrowser.open(url)
PY
