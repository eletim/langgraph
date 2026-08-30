.PHONY: all format lint type test smoke live-smoke

all: help

UV_CACHE_DIR ?= .uv-cache
export UV_CACHE_DIR
PYTEST_DISABLE_PLUGIN_AUTOLOAD ?= 1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD

PYTHON_FILES=src tests
TEST ?= .

test:
	uv run pytest $(TEST)

lint:
	uv run ruff check .
	uv run ruff format $(PYTHON_FILES) --diff
	uv run ruff check --select I $(PYTHON_FILES)
	uv run ty check src/coding_orchestrator

type:
	uv run ty check src/coding_orchestrator

format:
	uv run ruff format $(PYTHON_FILES)
	uv run ruff check --fix $(PYTHON_FILES)

smoke:
	uv run python -m coding_orchestrator.smoke

live-smoke:
	uv run python -m coding_orchestrator.live_smoke $(ARGS)

help:
	@echo 'format - run code formatters'
	@echo 'lint   - run linters and type checking'
	@echo 'test   - run tests'
	@echo 'smoke  - run a fake-client workflow smoke check'
	@echo 'live-smoke - run a PurpleMux lifecycle/E2E smoke check (set ARGS)'
