.PHONY: install server test lint format typecheck db-upgrade db-migrate db-downgrade refresh-data ui ui-install ui-build

install:
	pip install -e ".[dev]"

server:
	uvicorn fantasai.main:app --reload --port 8000

db-upgrade:
	alembic upgrade head

db-migrate:
	alembic revision --autogenerate -m "$(msg)"

db-downgrade:
	alembic downgrade -1

refresh-data:
	python scripts/refresh_data.py

test:
	pytest -v

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

typecheck:
	mypy src/

ui-install:
	npm install --prefix frontend

ui:
	npm run dev --prefix frontend

ui-build:
	npm run build --prefix frontend
