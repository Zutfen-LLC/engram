.PHONY: lint typecheck test check setup-python-dev

# Use virtual environment executables
VENV_BIN = .venv/bin

lint:
	$(VENV_BIN)/ruff check .

typecheck:
	$(VENV_BIN)/mypy engram/

test:
	$(VENV_BIN)/pytest -q

check: lint typecheck test
	@echo "All checks passed!"

setup-python-dev:
	bash scripts/setup-python-dev.sh
