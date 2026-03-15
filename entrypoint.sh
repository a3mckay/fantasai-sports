#!/bin/sh
set -e

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
