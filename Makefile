.PHONY: help install test lint format clean run

help:
	@echo "Available commands:"
	@echo "  make install    - Install dependencies"
	@echo "  make test       - Run tests"
	@echo "  make lint       - Run linter"
	@echo "  make format     - Format code"
	@echo "  make clean      - Clean generated files"
	@echo "  make run        - Run trading agent"

install:
	pip install -r requirements.txt

test:
	pytest

lint:
	flake8 src/ tests/
	mypy src/

format:
	black src/ tests/ examples/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache htmlcov .coverage build/ dist/

run:
	python main.py --config config/config.yaml --mode live

analyze:
	python main.py --config config/config.yaml --mode analyze

demo:
	python examples/demo_analysis.py

backtest:
	python examples/simple_backtest.py
