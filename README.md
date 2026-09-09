# Superset remediation

A nightly automation that finds real problems in a fork of
[Apache Superset](https://github.com/apache/superset), fixes them, proves the fix
with a command a human chose, and opens a pull request for a human to judge.

Two Devin Automations do the work: one scans, one remediates. A small service —
one process, one container, one SQLite file — watches what happened and reports
on it. What counts as a problem is not hardcoded: it lives as markdown in the
fork, and a human owns it.

**No pull request is ever merged automatically.**

---

## How to run

Two commands, whether you run them or your own Devin does.

```bash
export REPO=you/superset      # created by forking Superset if it does not exist
export DEVIN_API_KEY=...      # service user, Admin role
export DEVIN_ORG_ID=...
export GITHUB_TOKEN=...       # repo, issues, pull requests. Not merge.
make up                       # http://localhost:8000
make scan                     # optional: run now instead of waiting for 02:00 PT
```

There is no file to copy or fill in. The values are exported rather than passed
as `make up REPO=...` because three of them are secrets, and a command line ends
up in shell history and in `ps`.

`make up` bootstraps before it serves, idempotently: forks Superset, enables
Issues, creates the labels, seeds the classification registry, and creates the
playbook and both automations over REST, scoped to your fork.

Two grants no API exposes, both one-time:

- a **service-user** API key with the Admin role —
  https://app.devin.ai/settings/org-service-users — a personal key is rejected
  by the `/v3/organizations/*` endpoints this uses;
- **Devin's GitHub connection**, scoped to include your fork, so label and review
  events reach the automations. Bootstrap prints the link when it finishes.

The first scan finds no active classes and proposes some as a PR. Merging it is
what turns detection on.

To point your own Devin at this instead, give it the repo and this prompt;
[`AGENTS.md`](AGENTS.md) tells it the rest.

> Set up this repo against a fresh fork of Apache Superset in my org, run it, and
> show me the dashboard. Follow its AGENTS.md. Never merge anything.

Without Docker: `make install && make bootstrap && make run`. Every Make target
says who it is for in the comment above it.

---

## How to see measurements

| Path | |
|---|---|
| `/` | dashboard: funnel, rates, per-class table, live task list |
| `/report.md` | the same numbers as a write-up, sample size first |
| `/metrics` | Prometheus |
| `/metrics.json` | the same numbers as JSON |
| `/healthz` | 503 if the watch loop has gone stale |

Definitions are choices, so they are stated rather than implied:

- **Success** = a PR whose classification's validation command *passed*. A
  session that finished on a red build is not a success.
- **Autonomy** is measured over everything that settled *or* needed a human. Over
  successes alone it reads ~100%: the blocked session, the clearest possible loss
  of autonomy, would not be in the denominator.
- **Rejection is not failure.** Rejection judges the scanner — we fixed something
  nobody wanted. Failure judges the fixer.
- **The funnel is cumulative** ("ever reached"), not current state, which renders
  a finished run as a row of zeroes.
- **Merged** counts human decisions only. Nothing here can merge.
- **Rates are `null`, not `0`, when there is no data.** Zero reads as a measured
  failure.
- **Durations come from GitHub's timestamps**, not this process's clock, so a
  tracker started after the work reports the real elapsed time rather than the
  gap between its own first two observations.
- **Cost is measured or absent, never typed in.** Per-session spend is fetched
  from the consumption API, which is Enterprise-only; below that plan it returns
  no rows, and unmeasured cost is not reported at all — no ACU rows, no Cost
  section, no ACU series.

---

## The loop

```
  ★ = human-visible output        [H] = human decision; the loop waits here

  nightly schedule, or `make scan`
              │
              ▼
     ┌──────────────────┐   finding fits no class    ★ PR proposing
     │   scan session   │ ─────────────────────────▶    a new class
     │      (Devin)     │                                    │
     └────────┬─────────┘                       [H] merge it, mute it,
              │ finding fits an active class         or decline it
              ▼                                           │
     ★ GitHub issue,                                      ▼
       labelled devin:ready              .devin/classifications/
              │                          the registry: what counts as
              │                          a problem, and what proves a fix
              ▼                                           │
     ┌──────────────────┐ ◀───────────── read by ─────────┘
     │ remediate session│
     │      (Devin)     │  fix, then run the class's validate command
     └────────┬─────────┘
              ▼
     ★ pull request (never merged)
              │
      ┌───────┴─────────────────┬──────────────────────────┐
      ▼                         ▼                          ▼
  [H] merge it        [H] request changes        [H] label the issue
                       → attempt 2 on the           devin:rejected
                         same branch                → PR, branch and
                                                      issue closed

  Throughout: the tracker polls Devin and GitHub, records every transition,
  and serves ★ the dashboard, ★ report.md and ★ /metrics.
```

Every arrow out of the system ends at something a human can read in GitHub or on
the dashboard. Every `[H]` is a decision the automation will not make for you —
adopting a class, and accepting, rejecting or reworking a fix.

---

## Managing it

### Reviewing classification proposals

The scan opens a PR adding `<slug>.md` when it finds something no class covers.
Proposals are written to be merged unchanged: complete frontmatter,
`status: active`, a defensible severity, a `max_open` sized so the class drips
rather than floods, a validate command that runs in this repo, and the instances
that motivated it, with file and line.

| You think… | You do… |
|---|---|
| yes | merge the PR — detection starts on the next scan |
| yes, but not yet | change `status: active` to `muted`, then merge |
| no | move the file to `_declined/`, set `status: declined`, add a `## Why declined` section, merge |

Decline **on the proposal PR**, not by closing it. Closing is not a signal — the
class will be proposed again next run, because nothing was written down. That is
deliberate: the directory has to be the whole record, since a reader has the
files in front of them and not the PR history.

Editing a proposal before merging is always available. If proposals routinely
need rewriting, that is a bug in the scan prompt, not a step in the process.

### Adding a classification by hand

Commit a file to `.devin/classifications/` in the fork with the frontmatter
below. Nothing else is required — the next scan reads the directory. This is also
how you change what a class means: edit its file. Muting a noisy class is a
one-line edit, never a code change.

### Reviewing pull requests

| You want to… | You do… |
|---|---|
| accept the fix | merge it — the only way anything lands |
| ask for changes | request changes on the PR; that alone starts attempt 2 on the same branch |
| reject the finding | label the *issue* `devin:rejected` — the loop closes the PR, deletes the branch, closes the issue, and counts it as rejected rather than failed |
| add work by hand | open an issue and label it `devin:ready` |
| re-run a corrected issue | remove and re-apply the `devin:ready` label |

Read the PR as you would a colleague's. The validation command tells you the
class's own gate passed; it does not tell you the change was worth making.

---

## The classification registry

Issue classes live as markdown in the fork, not in this repo:

```
.devin/classifications/
  transitive-npm-advisory.md
  stale-python-lockfile.md
  _declined/test-describe-nesting.md   ← considered and rejected, with reasons
```

Three states, all readable from the directory alone: a file at the top level is
`active` or `muted`; a file in `_declined/` was rejected and is never proposed
again. `README.md` in that directory explains the lifecycle to whoever opens it.

### Structure of a classification file

```markdown
---
slug: transitive-npm-advisory
status: active          # active | muted | declined
severity: high          # critical | high | medium | low
max_open: 3             # cap on simultaneously open issues of this class
validate: cd superset-frontend && npm audit --audit-level=high
---

# One-line title

What the problem is and why it matters *in this repository's terms* — citing
`AGENTS.md`, `SECURITY.md`, `.cursor/rules/`, or the advisory.

## Recognise

What to run or read, what a genuine instance looks like as opposed to a false
positive, and an explicit exclusions list. A finding must be fixable by one
focused PR, so "237 files match" is a class, not a finding.

## Fix

The approach, the repo's conventions that apply, what must not change, and when
to stop and ask rather than guess.

## Validate

What the `validate:` command proves and what it does not.

## Seed finding (date)

The instances that motivated the class, with file and line — what makes a
proposal reviewable.
```

`validate:` is the load-bearing field. Every fix in the class is held to it, and
the dashboard flags any run whose actual command differed from it, so a session
cannot quietly grade its own homework. It must be non-interactive, must be
capable of failing on a bad fix, and should be the repo's own tooling. `<file>`
and `<scope>` in it are substituted with the issue's paths.

Not `pre-commit run`: hook environments install by cloning from github.com, which
the remediation sandbox's git proxy refuses, so the gate would fail for a reason
unrelated to the fix. Call the underlying tool directly.

A weak validation command is worse than no automation — it makes a broken fix
look verified.

---

## Layout

```
scanner/              finds problems, files issues, proposes new classes
remediator/           fixes them, validates the fix, opens a PR
tracker/              the only code that runs here: watch, record, report
shared/               Devin and GitHub clients, config
bootstrap/            one idempotent pass from nothing to a running system
docker/               Dockerfile + compose.yml
tests/                including one that asserts we cannot merge
```

`scanner/` and `remediator/` are definitions rather than code — the platform runs
them — so each holds its automation spec, the prompt it runs as prose, and the
output shape it is asked for. Where the Devin API behaved differently from its
documentation, the measured behaviour is written down beside the workaround in
`shared/devin.py`.

```bash
make check    # ruff + mypy + pytest
```
