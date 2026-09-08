.PHONY: install demo run check test lint types apply validate scan record clean

install:
	pip install -r requirements-dev.txt

# No credentials required: replays a recorded real run into the dashboard.
demo:
	MODE=replay DB_PATH=data/demo.db uvicorn src.app:app --host 0.0.0.0 --port 8000

run:
	MODE=live uvicorn src.app:app --host 0.0.0.0 --port 8000

# Validate the automation payloads against the live API. Creates nothing.
validate:
	python -m scripts.apply_automations --check

apply:
	python -m scripts.apply_automations

scan:
	python -m scripts.run_scan

record:
	python -m scripts.record_run --minutes $(or $(MINUTES),60)

lint:
	ruff check src scripts tests

types:
	mypy src scripts

test:
	pytest -q

check: lint types test

clean:
	rm -rf data .pytest_cache .mypy_cache .ruff_cache
