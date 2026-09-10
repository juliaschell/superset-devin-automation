# Every target takes the same four values, from the environment if they are
# exported there and from the command line otherwise:
#
#   export REPO=you/superset DEVIN_API_KEY=... DEVIN_ORG_ID=... GITHUB_TOKEN=...
#   make up
#
#   make up REPO=you/superset DEVIN_KEY=... DEVIN_ORG=... GITHUB_TOKEN=...
#
# Either name works for the Devin two. A command line lands in shell history
# and in `ps`, so prefer exporting the three secrets where that matters.

DEVIN_KEY := $(if $(DEVIN_KEY),$(DEVIN_KEY),$(DEVIN_API_KEY))
DEVIN_ORG := $(if $(DEVIN_ORG),$(DEVIN_ORG),$(DEVIN_ORG_ID))

export REPO
export GITHUB_TOKEN
export DEVIN_API_KEY := $(DEVIN_KEY)
export DEVIN_ORG_ID := $(DEVIN_ORG)
# The dashboard is http://superset.localhost. Override if :80 is taken, and it
# becomes http://superset.localhost:$(DASHBOARD_PORT).
export DASHBOARD_PORT ?= 80

COMPOSE = docker compose -f docker/compose.yml

.PHONY: up down scan logs values bootstrap run install check lint types test validate clean

# --- running it -------------------------------------------------------------

## Everyone. Bootstrap the fork and serve http://superset.localhost. Idempotent,
## so this is also how you apply a change to a prompt or an automation.
up: values
	$(COMPOSE) up --build

# Stop before starting anything if a value is missing. Usually a `:` typed
# instead of `=`, which make reads as the name of another target.
values:
	@missing="$(strip $(foreach v,REPO DEVIN_KEY DEVIN_ORG GITHUB_TOKEN,$(if $($(v)),,$(v))))"; \
	if [ -n "$$missing" ]; then \
		echo "no value for: $$missing"; \
		echo "export it (REPO, DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN) or pass it:"; \
		args=""; for v in $$missing; do args="$$args $$v=..."; done; \
		echo "  make $(or $(firstword $(MAKECMDGOALS)),up)$$args"; \
		exit 2; \
	fi

## Everyone. Run a scan now instead of waiting for 02:00 PT. Goes through the
## running container, which already has the dependencies and the credentials;
## falls back to this machine's Python when nothing is up (the no-Docker path).
scan:
	@if $(COMPOSE) ps --status running --quiet tracker | grep -q .; then \
		$(COMPOSE) exec -T tracker python -m scanner.run_now; \
	else \
		python -m scanner.run_now; \
	fi

## Everyone. Stop the tracker. State survives in the `state` volume.
down:
	$(COMPOSE) down

## Everyone. Follow the tracker's logs.
logs:
	$(COMPOSE) logs -f

# --- running it without Docker ----------------------------------------------

## Anyone working on this repo. Fork setup, playbook and both automations.
bootstrap: values
	python -m bootstrap

## Anyone working on this repo. The dashboard, without a container.
run:
	uvicorn tracker.app:app --host 0.0.0.0 --port $(DASHBOARD_PORT) --no-access-log

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
