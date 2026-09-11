# Superset remediation

A nightly loop over a fork of [Apache Superset](https://github.com/apache/superset):
Devin finds classes of defect, files them as GitHub issues, fixes them, proves
each fix with a validation command a human approved, and opens a PR.

One Devin automation scans, a second remediates, and a small service alongside
them polls Devin and GitHub and records every transition in a SQLite file.

**No pull request is ever merged automatically.**

---

## Run it

If you would rather have your own Devin drive this, point it at
[`AGENTS.md`](AGENTS.md); it is the whole procedure. To run it yourself:

### 1. Get the two credentials

- a Devin **service-user** key with the Admin role —
  https://app.devin.ai/settings/org-service-users
- a GitHub personal access token (classic) with the `repo` scope and nothing
  else — https://github.com/settings/tokens/new?scopes=repo opens the form with
  that box already ticked

### 2. Start the container

```bash
make up REPO=<you/fork> DEVIN_KEY=<key> DEVIN_ORG=<org_id> GITHUB_TOKEN=<token>
```

- `REPO` — the fork to work on, created for you if it does not exist. GitHub
  allows one fork of a repo per account, so if you already fork Superset, name
  that one
- `DEVIN_KEY` — the service-user key
- `DEVIN_ORG` — your Devin org id, `org-…`
- `GITHUB_TOKEN` — the `repo`-scoped token

Anything already exported is used as it is, so pass only what the environment
is missing — `make up` alone is enough when `REPO`, `DEVIN_API_KEY`,
`DEVIN_ORG_ID` and `GITHUB_TOKEN` are all exported. Prefer that: a command line
lands in shell history and in `ps`.

The container bootstraps before it serves: it forks Superset if needed, enables
Issues, creates the labels, seeds the classification registry, and creates the
playbook and both automations against your fork. It is idempotent, so this is
also how you apply a change to a prompt or an automation.

### 3. Give Devin access to the fork

The one step with no API, so bootstrap ends by printing it in a box: at
https://app.devin.ai/settings/integrations/github choose Configure / Manage
repositories and add your fork. Until you do, Devin cannot read the code and
label events reach no automation, so the loop looks idle. Access is per
repository — a fork you replace has to be added again.

### 4. Run the first scan

The scan runs at 02:00 PT on its own. To run one now, in a second terminal
(`make up` holds the first):

```bash
make scan
```

That opens an issue whose label is the trigger, so the scan starts on Devin's
side. `make scan` waits for that session and prints a link to it; if none
starts it says which of the two reasons applies — Devin has no access to the
fork, or the scan automation is over its 12-runs-a-day cap. From there the
`make up` terminal narrates the run, a line per state change, so nothing needs
the Devin session open.

### 5. Adopt at least one classification

The first scan finds the registry empty, so it files no issues and instead
opens a PR proposing classes. Review it, edit it if you want, and merge: the
scanner files nothing until at least one class is active.

### 6. Scan again

```bash
make scan
```

Every scan from here files issues for the active classes, and each issue
triggers a remediation session that opens a PR for you to review. New classes
may still be proposed alongside them.

The dashboard is at http://superset.localhost — browsers resolve any
`*.localhost` name to 127.0.0.1, so there is no `/etc/hosts` entry to add. If
port 80 is taken, `make up DASHBOARD_PORT=8000 ...` moves it to
http://superset.localhost:8000. The page reloads every 15 seconds and the loop
behind it polls just as often, so a change shows within about half a minute;
raise `POLL_INTERVAL_SECONDS` on a fork busy enough to feel GitHub's rate
limit.

**Without Docker**: `make install`, then `make bootstrap` and `make run` with
the same four values.

---

## What it measures

| Path | |
|---|---|
| `/` | dashboard: what is waiting on you, funnel, rates, per-class table, task list |
| `/report.md` | the same numbers as a write-up, sample size first |
| `/metrics` | Prometheus |
| `/metrics.json` | the same numbers as JSON |
| `/healthz` | 503 if the watch loop has gone stale |

The numbers are always about one fork. State lives in a Docker volume that
outlives the container, so `make up` with a different `REPO` clears it and the
new fork starts at zero.

Definitions are choices, so they are stated rather than implied:

- **Success** = a PR whose classification's validation command *passed*. A
  session that finished on a red build is not a success.
- **Autonomy** is measured over everything that settled *or* needed a human.
  Over successes alone it reads ~100%: a blocked session, the clearest possible
  loss of autonomy, would not be in the denominator.
- **Sent back** = a PR a human reviewed with *request changes*, over the PRs
  opened. Nothing here reacts to it — the PR's own session answers the review —
  so it is a clean read on how often a first attempt is not good enough.
- **Rejection is not failure.** Rejection judges the scanner: it filed
  something nobody wanted. Failure judges the fixer.
- **The funnel is cumulative** ("ever reached"), not current state, which would
  show a finished run as a row of zeroes.
- **Merged** counts human decisions only. Nothing here can merge.
- **Rates are `null`, not `0`, when there is no data.** A zero reads as a
  measured failure.
- **Durations come from GitHub's timestamps**, not this process's clock, so a
  tracker started after the work reports the real elapsed time rather than the
  gap between its own first two observations.
- **Cost is measured or absent, never typed in.** Per-session spend comes from
  the consumption API, which is Enterprise-only; below that plan it returns no
  rows and no cost is reported at all.

---

## The loop

```
  ★ = human-visible output

  cron, or `make scan`
              │
              ▼
     ┌──────────────────┐   finding fits no class    ★ PR proposing
     │      scanner     │ ─────────────────────────▶    a new class
     │      (Devin)     │                                     │
     └────────┬─────────┘                                     ▼
              │       │                                 human: review
              │       │                                       │
              │       ┘───────────────────────────────────────┘
              │                                               │
              │ finding fits an active class                  │
              ▼                                               │
     ★ GitHub issue,                                          │
       labelled devin:ready                                   │
              │                                               │
              ▼                                               │
     ┌──────────────────┐                                     │
     │ remediate session│                                     │
     │      (Devin)     │◀────────────────────────────────────┘
     └────────┬─────────┘
              ▼
     ★ Remediation PR
              │
      ┌───────┴─────────────┬──────────────────────┐
      ▼                     ▼                      ▼
  human: merge     human: comment        human: label issue
                  → the PR's own         devin:rejected
                    session revises it   → PR, branch and
                    (counted as rework)    issue closed


  Throughout: the tracker polls Devin and GitHub, records every transition,
  and serves ★ the dashboard, ★ report.md and ★ /metrics.
```

---

## The classification registry

What counts as a defect is yours to decide, so the classes live as markdown in
the **fork**, not in this repo:

```
.devin/classifications/
  README.md                            ← the lifecycle, in the fork
  transitive-npm-advisory.md
  stale-python-lockfile.md
  _declined/test-describe-nesting.md   ← considered and rejected, with reasons
```

A class file:

```markdown
---
slug: transitive-npm-advisory
status: active          # active | muted | declined
severity: high          # critical | high | medium | low
max_open: 3             # cap on simultaneously open issues of this class
validate: cd superset-frontend && npm audit --audit-level=high
---

# One-line title

## Recognise      what a real instance looks like, and what to exclude
## Fix            the approach, and what must not change
## Validate       what the command above does and does not prove
## Seed finding   the example that motivated the class, file and line
```

`validate:` is the load-bearing field: every fix in the class is held to it,
and the dashboard flags any run whose actual command differed, so a session
cannot quietly grade its own homework. What makes a good one is in
[`AGENTS.md`](AGENTS.md).

### Reviewing a proposal

The scan opens a PR adding `<slug>.md` whenever it finds something no active
class covers. Proposals are written to be merged unchanged.

| You think… | You do… |
|---|---|
| yes | merge the PR — detection starts on the next scan |
| yes, but not yet | change `status: active` to `muted`, then merge |
| no | move the file to `_declined/`, set `status: declined`, add a `## Why declined` section, merge |

Decline by recording the decline, not by closing the PR: the directory is the
only record, and a closed PR means the same class comes back next scan.

You need not edit a proposal by hand either. The PR belongs to a Devin session,
so a comment — *mute this one*, *decline this one, too noisy*, *drop this one*
— is enough for it to push the edits to the same branch. Merging stays your
click. If proposals routinely need rewriting, that is a bug in the scan prompt.

### Adding a class by hand

Commit a file in the shape above to `.devin/classifications/` in the fork; the
next scan reads the directory. This is also how you change what a class means.
If you have an agent write it, point it at [`AGENTS.md`](AGENTS.md), which
carries the rules a `validate:` command has to satisfy.

---

## Reviewing a remediation PR

| You want to… | You do… |
|---|---|
| accept the fix | merge it, edited by hand or not |
| ask for changes | comment on the PR. The session that opened it answers with commits to the same branch — nothing here re-triggers — and the tracker counts the PR as sent back |
| reject the finding | label the *issue* `devin:rejected`. The loop closes the PR, deletes the branch, closes the issue, and counts it as rejected rather than failed |
| add work by hand | open an issue and label it `devin:ready` |
| re-run a corrected issue | remove and re-apply the `devin:ready` label |

---

## Layout

```
scanner/              finds problems, files issues, proposes new classes
remediator/           fixes them, validates the fix, opens a PR
tracker/              polls both APIs, records state, serves the dashboard
shared/               Devin and GitHub clients, config
bootstrap/            one idempotent pass to set everything up
docker/               Dockerfile and compose.yml
tests/                including one asserting the code cannot merge a PR
```
