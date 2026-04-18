.PHONY: lint test check

lint:
	ruff check src tests

test:
	PYTHONPATH=src pytest -q

check: lint test
