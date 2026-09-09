# Every target takes the same four values, on the command line:
#
#   make up REPO=you/superset DEVIN_KEY=... DEVIN_ORG=... GITHUB_TOKEN=...
#
# Exporting them instead works too, under either name. Note that a command line
# lands in shell history and in `ps`, so export the three secrets on a machine
# where that matters.

DEVIN_KEY := $(if $(DEVIN_KEY),$(DEVIN_KEY),$(DEVIN_API_KEY))
DEVIN_ORG := $(if $(DEVIN_ORG),$(DEVIN_ORG),$(DEVIN_ORG_ID))

export REPO
export GITHUB_TOKEN
export DEVIN_API_KEY := $(DEVIN_KEY)
export DEVIN_ORG_ID := $(DEVIN_ORG)

COMPOSE = docker compose -f docker/compose.yml

.PHONY: up down scan logs bootstrap run install check lint types test validate clean

# --- running it -------------------------------------------------------------

## Everyone. Bootstrap the fork and serve the dashboard on :8000. Idempotent,
## so this is also how you apply a change to a prompt or an automation.
up:
	$(COMPOSE) up --build

## Everyone. Run a scan now instead of waiting for 02:00 PT.
scan:
	python -m scanner.run_now

## Everyone. Stop the tracker. State survives in the `state` volume.
down:
	$(COMPOSE) down

## Everyone. Follow the tracker's logs.
logs:
	$(COMPOSE) logs -f

# --- running it without Docker ----------------------------------------------

## Anyone working on this repo. Fork setup, playbook and both automations.
bootstrap:
	python -m bootstrap

## Anyone working on this repo. The dashboard, without a container.
run:
	uvicorn tracker.app:app --host 0.0.0.0 --port 8000

# --- working on this repo ---------------------------------------------------

## Contributors. Runtime and dev dependencies, from pyproject.toml.
install:
	pip install -e ".[dev]"

## Contributors, and CI. Everything that has to be green.
check: lint types test

lint:
	ruff check .

types:
	mypy bootstrap tracker scanner shared

test:
	pytest -q

## Contributors. Reachability of every trigger, against the live Devin API.
## Creates and changes nothing.
validate:
	python -m bootstrap.playbooks --check
	python -m bootstrap.automations --check

clean:
	rm -rf data .pytest_cache .mypy_cache .ruff_cache
