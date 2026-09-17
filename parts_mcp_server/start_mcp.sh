#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "请先运行 ./install.sh"
  exit 1
fi
exec .venv/bin/python xiaozhi_bridge.py
