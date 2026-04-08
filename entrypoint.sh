#!/bin/sh
set -e

# Short TCP connect timeout so each failed attempt takes 5s instead of 4 minutes.
# This lets all 15 retries complete well within Railway's 5-minute healthcheck window.
export PGCONNECT_TIMEOUT=5

echo "Waiting for database and running migrations..."
for i in $(seq 1 15); do
    if alembic upgrade head; then
        echo "Migrations complete."
        break
    fi
    echo "Migration attempt $i failed (DB may be starting). Retrying in 5s..."
    sleep 5
done

exec uvicorn fantasai.main:app --host 0.0.0.0 --port "${PORT:-8000}"
