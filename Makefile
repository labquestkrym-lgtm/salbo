# Developer entry points. Uses the local venv if present, else system python.
PY ?= ./.venv/Scripts/python.exe

.PHONY: install fmt lint type test check up down logs

install:
	python -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

fmt:
	$(PY) -m ruff format app tests

lint:
	$(PY) -m ruff check app tests

type:
	$(PY) -m mypy app

test:
	$(PY) -m pytest

# Run the full quality gate (used in CI and before commits).
check: fmt lint type test

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api
