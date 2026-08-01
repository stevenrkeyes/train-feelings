#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

export DATA_SOURCE="${DATA_SOURCE:-local}"
export COLLECTOR_ENABLED="${COLLECTOR_ENABLED:-true}"
export DATA_DIR="${DATA_DIR:-./data}"
export DATABASE_PATH="${DATABASE_PATH:-./data/trains.db}"

exec uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
