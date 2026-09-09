# Superset remediation

A nightly automation that finds problem classes in a fork of
[Apache Superset](https://github.com/apache/superset), files real git bugs, fixes them, proves the fix with a human-approved validation command, and opens a PR with the proposed fix.

The system includes a Devin automation to scan for problems and a separate automation to remdiate the problems. An external service polls both processes and records data and metrics in an SQLite file. 

**No pull request is ever merged automatically.**

---

## How to run

***NOTE:*** If you would like your own Devin to manage this process, point it at [`AGENTS.md`](AGENTS.md) and it can guide you through. If you want to run it yourself, follow the steps below. 

1. Retrieve authentication tokens: 

- a **service-user** API key with the Admin role —
  https://app.devin.ai/settings/org-service-users 
- A GitHub API token <fill in link or click-instructions here> 

2. Stand up the container:

```bash
$ make up REPO=<username/fork_name> DEVIN_KEY=<key> DEVIN_ORG=<org_name> GITHUB_TOKEN=<token>
```

- `REPO` — the fork to work on, created for you if it does not exist yet
- `DEVIN_KEY` — a Devin **service-user** key, Admin role
- `DEVIN_ORG` — your Devin org id
- `GITHUB_TOKEN` — repo, issues, pull requests. Not merge

The container will boot-strap as needed (create the fork, modify git settings, seed the classification registry, and create the playbook and automations for the fork)

3. Configure Devin's GitHub access: 

If needed, bootstrap will print a link for you to enable label and review events 

4. Start the scanner by hand: 

```bash
$ make scan
```

If not manually kicked, it would run automatically at 2:00PT

The scanner will create git issues which will trigger the remediation automation to post fix PRs

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
     │      (Devin)     │                                    │
     └────────┬─────────┘                                    │
              │ finding fits an active class                 │
              ▼                                              │
     ★ GitHub issue,                                         ▼
       labelled devin:ready                             human: review 
              │                                              │
              ▼                                              │
     ┌──────────────────┐ ◀───────────────────────────────---┘
     │ remediate session│
     │      (Devin)     │
     └────────┬─────────┘
              ▼
     ★ Remediation PR 
              │
      ┌───────┴─────────────────┬────────────────────────────────┐
      ▼                         ▼                                ▼
  human: merge       human: request changes      human: label issue devin:rejected
                       → attempt 2 on the          → PR, branch and issue closed
                         same branch                
                                                      

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

Commit a file to `.devin/classifications/` in the fork with the frontmatter
below. Nothing else is required — the next scan reads the directory. This is also
how you change what a class means.

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
```

`validate:` is the load-bearing field. Every fix in the class is held to it, and the dashboard flags any run whose actual command differed from it, so a session cannot quietly grade its own homework. It must be non-interactive, must be capable of failing on a bad fix, and should be the repo's own tooling. 

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
