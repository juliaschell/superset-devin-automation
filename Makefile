# Every target takes the same four values, exported or passed in:
#
#   export REPO=you/superset DEVIN_API_KEY=... DEVIN_ORG_ID=... GITHUB_TOKEN=...
#   make up
#
#   make up REPO=you/superset DEVIN_KEY=... DEVIN_ORG=... GITHUB_TOKEN=...
#
# Either name works for the Devin two. A command line lands in shell history
# and in `ps`, so prefer exporting the secrets where that matters.

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

# Bootstrap the fork and serve the dashboard. Idempotent, so this is also how
# a change to a prompt or an automation is applied.
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

# Scan now instead of at 02:00 PT. Through the running container, which has
# the dependencies and the credentials; local Python when nothing is up.
scan:
	@if $(COMPOSE) ps --status running --quiet tracker | grep -q .; then \
		$(COMPOSE) exec -T tracker python -m scanner.run_now; \
	else \
		python -m scanner.run_now; \
	fi

# Stop the tracker. State survives in the `state` volume.
down:
	$(COMPOSE) down

# Follow the tracker's logs.
logs:
	$(COMPOSE) logs -f

# --- running it without Docker ----------------------------------------------

# Fork setup, playbook and both automations.
bootstrap: values
	python -m bootstrap

# The dashboard, without a container.
run:
	uvicorn tracker.app:app --host 0.0.0.0 --port $(DASHBOARD_PORT) --no-access-log

# --- working on this repo ---------------------------------------------------

# Runtime and dev dependencies, from pyproject.toml.
install:
	pip install -e ".[dev]"

# Everything that has to be green.
check: lint types test

lint:
	ruff check .

types:
	mypy bootstrap tracker scanner shared

test:
	pytest -q

# Check every trigger against the live Devin API. Creates and changes nothing.
validate:
	python -m bootstrap.playbooks --check
	python -m bootstrap.automations --check

clean:
	rm -rf data .pytest_cache .mypy_cache .ruff_cache
