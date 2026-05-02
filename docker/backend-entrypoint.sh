#!/bin/sh
# Run pending Alembic migrations before starting the app.
# Skip with RUN_MIGRATIONS=0 if a separate job owns schema management.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

# Use Railway's $PORT if set, otherwise default to 8000
exec uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --proxy-headers
