FROM python:3.11-slim

WORKDIR /app

# Install system deps needed by psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Cache pip dependencies: copy only the manifest, create a minimal package
# stub so pip can resolve deps, then install. This layer is only rebuilt
# when pyproject.toml changes.
COPY pyproject.toml README.md ./
RUN mkdir -p src/fantasai && touch src/fantasai/__init__.py \
    && pip install --no-cache-dir -e . \
    && rm -rf src/

# Copy full application source and reinstall in editable mode (no dep download)
COPY . .
RUN pip install --no-cache-dir -e . --no-deps

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
CMD ["/entrypoint.sh"]
