# FantasAI Sports

## Overview
MLB fantasy baseball assistant. Three-layer architecture: Engine (data + scoring), Brain (league intelligence + recommendations), Interface (API + future UI).

## Commands
- `make install` — install package in editable mode with dev deps
- `make server` — run FastAPI dev server on port 8000
- `make test` — run pytest
- `make lint` / `make format` — ruff check/format
- `make db-upgrade` — run Alembic migrations
- `make db-migrate msg="description"` — generate new migration

## Architecture
- **Sport Adapter pattern**: `src/fantasai/adapters/base.py` defines the interface, `mlb.py` implements for MLB via pybaseball
- **Engine** (`src/fantasai/engine/`): scoring and ranking logic, sport-agnostic
- **Brain** (`src/fantasai/brain/`): league-aware recommendations
- **API** (`src/fantasai/api/`): FastAPI routes, versioned under `/api/v1/`

## Code Conventions
- Python 3.9+, use `from __future__ import annotations` in every file
- Type hints everywhere
- SQLAlchemy 2.0 style (`mapped_column`, `DeclarativeBase`)
- Pydantic v2 schemas with `ConfigDict(from_attributes=True)`
- Stats stored as JSON columns (not individual columns) — pybaseball returns 300+ columns per player type
- Player IDs: FanGraphs `IDfg` as primary key, `key_mlbam` stored for Statcast cross-referencing

## Environment
- Secrets in `.env` file (never committed). See `.env.example` for template.
- PostgreSQL on Railway for production. SQLite in-memory for tests.

## PRD
Full product requirements in `fantasy-sports-helperPRD.md` at repo root.
