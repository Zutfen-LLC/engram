.PHONY: lint typecheck test check

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
