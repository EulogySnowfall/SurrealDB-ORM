.DEFAULT_GOAL := all
sources = src tests

.PHONY: .uv  # Check that uv is installed
.uv:
	@uv --version || echo 'Please install uv: https://docs.astral.sh/uv/getting-started/installation/'

.PHONY: install  # Install the package, dependencies, and pre-commit for local development
install: .uv
	uv sync --frozen --group lint
	uv run pre-commit install --install-hooks

.PHONY: format  # Format the code
format:
	uv run ruff format
	uv run ruff check --fix --fix-only

.PHONY: lint  # Lint the code
lint:
	uv run ruff format --check
	uv run ruff check

.PHONY: typecheck
typecheck:
	uv run pyright

.PHONY: test
test:
	uv run pytest

.PHONY: test-all-python  # Run tests on Python 3.11 to 3.13
test-all-python:
	uv run --python 3.11 coverage run -p -m pytest --junitxml=junit.xml -o junit_family=legacy
	uv run --python 3.12 coverage run -p -m pytest
	uv run --python 3.13 coverage run -p -m pytest
	@uv run coverage combine
	@uv run coverage xml -o coverage.xml
	@uv run coverage report

.PHONY: all
all: format lint typecheck test-all-python