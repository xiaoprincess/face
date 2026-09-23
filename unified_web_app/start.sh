#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/gunicorn ]; then
    echo "请先运行: sh install.sh" >&2
    exit 1
fi
exec .venv/bin/gunicorn --workers 1 --threads 50 --bind 127.0.0.1:5050 app:app
