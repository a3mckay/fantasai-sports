FROM python:3.11-slim

WORKDIR /app

# Install system deps needed by psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first so this layer is cached between code-only changes
COPY pyproject.toml README.md ./
COPY src/fantasai/__init__.py src/fantasai/__init__.py

RUN pip install --no-cache-dir -e .

# Now copy the rest of the source
COPY . .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
