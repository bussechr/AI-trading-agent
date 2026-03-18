.PHONY: test lint run-api

test:
	python -m pytest

lint:
	ruff check src tests scripts

run-api:
	uvicorn fxstack.api.app:app --host 127.0.0.1 --port 58710 --reload
