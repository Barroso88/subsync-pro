#!/usr/bin/env bash
# Arranque local (sem Docker). Uso: ./run-local.sh [pasta-de-media]
# A pasta indicada (por omissão a sua home) fica disponível para registar bibliotecas.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="${1:-$HOME}"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r backend/requirements.txt
if [ ! -f frontend/dist/index.html ]; then
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi
export SUBSYNC_DATA_DIR="$PWD/data" SUBSYNC_ALLOWED_ROOTS="$ROOT" SUBSYNC_STATIC_DIR="$PWD/frontend/dist"
echo ">> SubSync Pro em http://localhost:8484  (media: $ROOT)"
exec .venv/bin/python -m uvicorn app.main:app_factory --factory --app-dir backend --host 127.0.0.1 --port 8484
