.PHONY: install bootstrap run check test lint types apply validate scan clean

install:
	pip install -r requirements-dev.txt

# Fork Superset if needed, set the repo up, and create the playbook and both
# automations against it. Idempotent: safe to re-run.
bootstrap:
	python -m scripts.bootstrap

run:
	uvicorn src.app:app --host 0.0.0.0 --port 8000

# Validate the automation payloads against the live API. Creates nothing.
validate:
	python -m scripts.apply_playbooks --check
	python -m scripts.apply_automations --check

# Playbooks first: an automation prompt references its playbook by id, so the
# playbook has to exist before the automation that points at it.
apply:
	python -m scripts.apply_playbooks
	python -m scripts.apply_automations

scan:
	python -m scripts.run_scan

lint:
	ruff check src scripts tests

types:
	mypy src scripts

test:
	pytest -q

check: lint types test

clean:
	rm -rf data .pytest_cache .mypy_cache .ruff_cache
