#!/bin/sh
# Run pending Alembic migrations before starting the app.
# Skip with RUN_MIGRATIONS=0 if a separate job owns schema management.
set -e

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

exec "$@"
