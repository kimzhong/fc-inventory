.PHONY: help install run lint type test cov docker build clean

# ── Help ───────────────────────────────────────────────────
help:
	@echo "fc-inventory v3.0.0 — Makefile targets:"
	@echo "  install   pip install -e .[dev]"
	@echo "  run       uvicorn app.main:app --reload"
	@echo "  lint      ruff check ."
	@echo "  type      mypy app"
	@echo "  test      pytest"
	@echo "  cov       pytest --cov=app --cov-report=term-missing"
	@echo "  docker    docker build -t fc-inventory:dev ."
	@echo "  build     python -m build"
	@echo "  clean     remove caches, dist, __pycache__"

# ── Install ─────────────────────────────────────────────────
install:
	python -m pip install --upgrade pip
	pip install -e ".[dev]"

# ── Run dev server ─────────────────────────────────────────
run:
	uvicorn app.main:app --reload --host 127.0.0.1 --port 5000

# ── Lint / type-check / test ───────────────────────────────
lint:
	ruff check .

type:
	mypy app

test:
	pytest --no-cov

cov:
	pytest

# ── Docker ──────────────────────────────────────────────────
docker:
	docker build -t fc-inventory:dev .

# ── Build sdist + wheel ───────────────────────────────────
build:
	python -m build

# ── Clean ───────────────────────────────────────────────────
clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache/ .mypy_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
