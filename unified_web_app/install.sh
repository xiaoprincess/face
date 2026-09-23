#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
