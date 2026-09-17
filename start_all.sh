#!/usr/bin/env bash
set -euo pipefail

face_root="$(cd "$(dirname "$0")" && pwd)"
web_root="$face_root/unified_web_app"
mcp_root="$face_root/parts_mcp_server"

# Reuse the already calibrated Raspberry Pi pan/tilt package in web/ without
# copying or changing its motorDrive implementation.
export PYTHONPATH="$face_root/web:$face_root/web/agent_yuntai${PYTHONPATH:+:$PYTHONPATH}"

if [ ! -x "$web_root/.venv/bin/gunicorn" ]; then
  echo "Website environment is missing. Run unified_web_app/install.sh first." >&2
  exit 1
fi
if [ ! -f "$web_root/.env" ]; then
  echo "Website configuration is missing. Create unified_web_app/.env first." >&2
  exit 1
fi
if [ ! -x "$mcp_root/.venv/bin/python" ]; then
  echo "MCP environment is missing. Run parts_mcp_server/install.sh first." >&2
  exit 1
fi
if [ ! -f "$mcp_root/.env" ]; then
  echo "MCP configuration is missing. Create parts_mcp_server/.env first." >&2
  exit 1
fi

web_pid=""
mcp_pid=""

cleanup() {
  if [ -n "$web_pid" ]; then
    kill "$web_pid" 2>/dev/null || true
  fi
  if [ -n "$mcp_pid" ]; then
    kill "$mcp_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

(cd "$web_root" && exec .venv/bin/gunicorn --workers 1 --threads 50 --bind 127.0.0.1:5050 app:app) &
web_pid=$!
(cd "$mcp_root" && exec .venv/bin/python -X utf8 xiaozhi_bridge.py) &
mcp_pid=$!

echo "Website and MCP started. Press Ctrl+C to stop both."
wait -n "$web_pid" "$mcp_pid"
