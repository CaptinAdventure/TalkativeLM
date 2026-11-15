#!/bin/sh
set -eu

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

. .venv/bin/activate

if ! python -c "import django, django_mcp, uvicorn" >/dev/null 2>&1; then
    python -m pip install -r requirements.txt
fi

exec python -m uvicorn talkative.asgi:application --host 0.0.0.0 --port 8000 "$@"
