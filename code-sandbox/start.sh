#!/usr/bin/env bash
set -euo pipefail

# Jupyter im Hintergrund (fuer OWUI Code-Interpreter)
# - token-geschuetzt
# - allow_origin/xsrf-Lockerung, damit OWUI die Kernel-API ansprechen kann
# - lauscht auf allen Interfaces (nur im internen sandbox-net erreichbar)
jupyter lab \
  --ServerApp.ip=0.0.0.0 \
  --ServerApp.port=8888 \
  --ServerApp.token="${JUPYTER_TOKEN:-changeme}" \
  --ServerApp.password='' \
  --ServerApp.allow_origin='*' \
  --ServerApp.disable_check_xsrf=True \
  --ServerApp.root_dir=/home/sandbox/work \
  --no-browser \
  --ServerApp.open_browser=False &

# /run-API im Vordergrund (fuer den Agent)
exec uvicorn run_api:app --host 0.0.0.0 --port 8000
