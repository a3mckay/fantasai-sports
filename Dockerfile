FROM python:3.11-slim

WORKDIR /app

# Install system deps needed by psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir -e .

CMD alembic upgrade head && uvicorn fantasai.main:app --host 0.0.0.0 --port ${PORT:-8000}
