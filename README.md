# Superset remediation

A nightly automation that finds problem classes in a fork of
[Apache Superset](https://github.com/apache/superset), files real bugs as GitHub issues, fixes them, proves the fix with a human-approved validation command, and opens a PR with the proposed fix.

The system includes a Devin automation to scan for problems and a separate automation to remediate them. A small service alongside them polls Devin and GitHub and records data and metrics in an SQLite file.

**No pull request is ever merged automatically.**

---

## How to run

***NOTE:*** If you would like your own Devin to manage this process, point it at [`AGENTS.md`](AGENTS.md) and it can guide you through. If you want to run it yourself, follow the steps below. 

1. Retrieve authentication tokens: 

- a **service-user** API key with the Admin role —
  https://app.devin.ai/settings/org-service-users 
- a personal access token (classic) with the `repo` scope, and only that one —
  https://github.com/settings/tokens/new?scopes=repo opens the form with the
  box already ticked

2. Stand up the container:

```bash
$ make up REPO=<username/fork_name> DEVIN_KEY=<key> DEVIN_ORG=<org_id> GITHUB_TOKEN=<token>
```

- `REPO` — the fork to work on, created for you if it does not exist yet
- `DEVIN_KEY` — a Devin **service-user** key, Admin role
- `DEVIN_ORG` — your Devin org id, `org-…`
- `GITHUB_TOKEN` — the `repo` scope. Nothing here ever merges

The container will boot-strap as needed (create the fork, modify git settings, seed the classification registry, and create the playbook and automations for the fork)

3. Configure Devin's GitHub access: 

Bootstrap finishes by printing a link to connect the fork to Devin. The UI grant
is required for label and review events to trigger Devin Automations.

4. Start the scanner by hand: 

In a second terminal, since `make up` holds the first one:

```bash
$ make scan
```

If not manually kicked, it would run automatically at 2:00PT

The scanner will create GitHub issues which will trigger the remediation automation to post fix PRs

A metrics dashboard will be available at http://localhost:8000

5. Merge 1+ classifications

The first scan will find the classification directory empty, so it will not file bugs. It will propose some new classifications in your fork repo. These PRs must be reviewed, modified as desired, and merged. There must be at least 1 active classification for the scanner to be able to file new bugs. 

6. Continue the scanning loop 

```bash
$ make scan
```
Each following scan will file bugs according to the existing classifications, and may propose new classification PRs to review. 

---

***Run without Docker***: `make install`, then `make bootstrap` and `make run` with the
same four values. Every Make target says who it is for in the comment above it.

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
  human: merge   human: request changes   human: label issue  
                  → attempt 2 on the      devin:rejected      
                    same branch           → PR, branch and    
                                            issue closed      


  Throughout: the tracker polls Devin and GitHub, records every transition,
  and serves ★ the dashboard, ★ report.md and ★ /metrics.
```

---

## Managing it

### Reviewing classification proposals

The scan opens a PR adding `<slug>.md` when it finds a potential bug without an existing classification.
Proposals are written to be merged unchanged (state, settings, validation command), but you can modify them as needed in that PR or any later PR. 

| You think… | You do… |
|---|---|
| yes | merge the PR — detection starts on the next scan |
| yes, but not yet | change `status: active` to `muted`, then merge |
| no | move the file to `_declined/`, set `status: declined`, add a `## Why declined` section, merge |

Decline suggested classifications **by setting the status and moving to declined folder**, not by closing the PR or removing the file. The classification directory is ground truth. Closing is not a signal — the class will be proposed again next run. 

Editing a proposal before merging is always available. If proposals routinely
need rewriting, that is a bug in the scan prompt, not a step in the process.

### Adding a classification by hand

Commit a file to `.devin/classifications/` in the fork in the shape below.
Nothing else is required — the next scan reads the directory. This is also how
you change what a class means. If you have an agent write it, point it at
[`AGENTS.md`](AGENTS.md), which carries the rules a `validate:` command has to
satisfy.

---

## The classification registry

Issue classes live as markdown in the fork, not in this repo:

```
.devin/classifications/
  README.md                            ← see for details on the lifecycle of a classification 
  transitive-npm-advisory.md
  stale-python-lockfile.md
  _declined/test-describe-nesting.md   ← considered and rejected, with reasons
```

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

## Recognise      what a real instance looks like, and what to exclude
## Fix            the approach, and what must not change
## Validate       what the command above does and does not prove
## Seed finding   the example that motivated the class, file and line
```

`validate:` is the load-bearing field: every fix in the class is held to it, and
the dashboard flags any run whose actual command differed, so a session cannot
quietly grade its own homework. What makes a good one is in
[`AGENTS.md`](AGENTS.md).

---

### Reviewing Remediation pull requests

| You want to… | You do… |
|---|---|
| accept the fix | merge it (with or without modifying by hand) |
| ask Devin for changes | request changes on the PR; that alone starts attempt 2 on the same branch |
| reject the finding | label the *issue* `devin:rejected` — the loop closes the PR, deletes the branch, closes the issue, and counts it as rejected rather than failed |
| add work by hand | open an issue and label it `devin:ready` |
| re-run a corrected issue | remove and re-apply the `devin:ready` label |


---

## Layout

```
scanner/              finds problems, files issues, proposes new classes
remediator/           fixes them, validates the fix, opens a PR
tracker/              records persistent cross-session data, presents metrics 
shared/               Devin and GitHub clients, config
bootstrap/            one pass to set up the environment
docker/               Dockerfile + compose.yml
tests/                including one that asserts we cannot merge
```
