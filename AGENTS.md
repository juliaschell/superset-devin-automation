# Instructions for an agent operating this repository

This repository runs a Devin-driven detect → fix → verify loop on a fork of
Apache Superset. If someone has pointed you at this repo and asked you
to run it, this file is the whole procedure.

## The one rule

**Never merge a pull request** — not here, not on the fork, not the class
proposals the scan opens. Every merge in this system is a human decision, and
the metrics only mean something because that is true. Say what is mergeable and
leave it.

## What you need from the person who asked

Ask once, together, and do not guess or substitute your own:

| Variable | What it is |
|---|---|
| `REPO` | the Superset fork to work on, as `owner/name`. It need not exist yet |
| `DEVIN_KEY` | a **service-user** key with the Admin role — `/v3/organizations/*` rejects a personal key |
| `DEVIN_ORG` | their org id |
| `GITHUB_TOKEN` | a classic token with the `repo` scope, and only that one |

There is no file to create and no `.env` to leave lying around. Never print them
and never commit them.

## Running it

```bash
make up REPO=owner/superset DEVIN_KEY=... DEVIN_ORG=... GITHUB_TOKEN=...
make scan    # optional, second terminal: run a scan now, not at 02:00 PT
```

That serves http://localhost:8000. `export`ing the four instead works and keeps
the secrets out of shell history and `ps`; on a shared machine, prefer it.
`make scan` goes through the running container, so it needs nothing installed
and no values re-passed.

`make up` runs `python -m bootstrap` first, which forks Superset if
`REPO` is missing, enables Issues, creates the labels, seeds the classification
registry, and creates the playbook and both automations over REST scoped to that
fork. It is idempotent — re-running reconciles rather than duplicates, so prefer
re-running it to hand-repairing anything it created.

One thing you cannot do for them: adding the fork to Devin's GitHub access
(https://app.devin.ai/settings/integrations/github → Configure / Manage
repositories) is a UI grant with no API, and it is per repository — a fork they
replace has to be added again. Until it is done, label and review events never
reach the automations and the loop looks silently idle. Tell them, and check it
first if nothing fires: `make scan` fails with these instructions when the
session it triggered never starts.

The other reason nothing starts is a cap, not a fault: each automation limits
its own runs (`limits.invocations` in `scanner/automation.json` and
`remediator/automation.json`), and a trigger over the cap is recorded as a
**skipped** invocation — the issue is labelled, the automation is enabled, and
no session exists. `make scan` distinguishes the two and names the cap. Raise
the number there and re-run `make up` to apply it.

## What to expect, in order

1. The first scan finds no active classifications, so it files **no issues** and
   instead opens a PR proposing classes. That is correct behaviour, not a
   failure. The human merges it to switch detection on.
2. The next scan files issues for the adopted classes and labels them
   `devin:ready`, which triggers the remediation automation.
3. Each remediation session opens a PR that has passed its class's validation
   command. Nothing merges them.
4. The dashboard at `http://localhost:8000` shows the funnel; `/report.md` is
   the same thing written out, and `/healthz` fails if the watch loop stalls.

Metrics are per fork: the SQLite volume records which `REPO` it describes and
empties itself when that changes, and sessions carry a `repo:` tag so one
fork's work never counts toward another's. Nothing to reset by hand.

The `make up` terminal prints one line per state change — a scan starting and
what it produced, an issue detected, a session dispatched, a PR rejected — so
tailing it is enough to follow a run, and silence means nothing has changed.

Detection is only as good as the registry, so if the person wants to see the
loop close in one sitting, the useful thing to do is help them review the class
proposals — not to add classes yourself.

## Changing things

- One directory per system: `scanner/` and `remediator/` each hold the
  automation spec, the prompt and the output schema that define them;
  `tracker/` is the service, `shared/` its clients, `bootstrap/` the
  setup pass. Change behaviour by editing those files and re-running bootstrap.
  Do not edit automations in the Devin UI — the next bootstrap would overwrite
  the change and the diff would exist nowhere.
- The classification registry lives in the **fork**, not here, and belongs to
  the human. Propose via PR; never move a file out of `_declined/`.
- `make check` runs ruff, mypy and the tests. Keep it green.

## Writing a classification, if you are asked to

The file's shape is in the README. Everything load-bearing is in `validate:`,
the command every fix in the class is held to, so:

- it must run non-interactively and must be able to **fail** on a bad fix — a
  command that always passes makes a broken fix look verified;
- `<file>`, `<files>` and `<scope>` in it are substituted with the issue's paths
  before the session runs it, which is why the tracker's "different command"
  check does not fire on them;
- never `pre-commit run`: its hook environments install by cloning from
  github.com, which the remediation sandbox's git proxy refuses, so the gate
  fails for a reason unrelated to the fix. Call the underlying tool directly.
