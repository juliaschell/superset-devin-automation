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
- a personal access token with the `repo` scope —
  https://github.com/settings/tokens/new?scopes=repo 

2. Stand up the container:

```bash
$ make up REPO=<username/fork_name> DEVIN_KEY=<key> DEVIN_ORG=<org_id> GITHUB_TOKEN=<token>
```

- `REPO` — the fork to work on, created for you if it does not exist yet. GitHub
  allows one fork of a repo per account, so if you already fork Superset, give
  that name here. Otherwise, supply the name you would like Devin to use for the new fork. 
- `DEVIN_KEY` — a Devin **service-user** key, Admin role
- `DEVIN_ORG` — your Devin org id, `org-…`
- `GITHUB_TOKEN` — the `repo` scope. Nothing here ever merges

The container will boot-strap as needed (create the fork, modify git settings, seed the classification registry, and create the playbook and automations for the fork)

3. Configure Devin's GitHub access: 

Bootstrap prints a boxed warning to configure GitHub integration with Devin for your repo
    - https://app.devin.ai/settings/integrations/github 
    - choose Configure / Manage repositories and ensure your fork is already listed or add your fork
    
There may be no action required if you already have Devin configured with access to all of your GitHub repositories. 

4. Start the scanner by hand: 

In a second terminal, since `make up` holds the first one:

```bash
$ make scan
```
If not manually kicked, it would run automatically at 2:00PT

5. Merge 1+ classifications

The first scan will find the classification directory empty, so it will not file bugs. It will propose some new classifications in your fork repo. These PRs must be reviewed, modified as desired, and merged. There must be at least 1 active classification for the scanner to be able to file new bugs. 

6. Continue the scanning loop 

```bash
$ make scan
```
Each following scan will file bugs according to the existing classifications, and may propose new classification PRs to review. 

7. Observe the dashboard 

Available at http://superset.localhost while the container is up 

---

## How to see measurements

| Path | |
|---|---|
| `/` | dashboard: what is waiting on you, funnel, rates, per-class table, live task list |
| `/report.md` | the same numbers as a write-up, sample size first |
| `/metrics` | Prometheus |
| `/metrics.json` | the same numbers as JSON |
| `/healthz` | 503 if the watch loop has gone stale |

The numbers are always about one fork: state lives in a docker volume that
outlives the container, so starting `make up` with a different `REPO` clears it
and a fresh fork begins at zero.

Definitions are choices, so they are stated rather than implied:

- **Success** = a PR whose classification's validation command *passed*. A
  session that finished on a red build is not a success.
- **Autonomy** = measured over everything that settled *or* needed a human. Over
  successes alone it reads ~100%: the blocked session, the clearest possible loss
  of autonomy, would not be in the denominator.
- **Sent back** = a PR a human reviewed with *request changes*, over the PRs
  opened
- **Rejection is not failure.** Rejection judges the scanner — we fixed something
  nobody wanted. Failure judges the remediator.
- **The funnel is cumulative** ("ever reached"), not current state, which renders
  a finished run as a row of zeroes.
- **Rates are `null`, not `0`, when there is no data.** 
- **Durations come from GitHub's timestamps**, not this process's clock
- **Cost is measured or absent** Per-session spend is fetched
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
     ┌──────────────────┐   finding fits no class     ★ PR proposing
     │      scanner     │ ─────────────────────────▶  classifications
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

## Managing it

### Reviewing classification proposals

The scan opens a PR adding `<slug>.md` when it finds a potential bug without an existing classification.
Proposals are written to be merged unchanged (state, settings, validation command), but you can modify them as needed in that PR or any later PR. 

if... 
- *The PR is good as-is*: merge the PR — detection starts on the next scan
- *The classification is relevant but currently should be skipped*: change `status: active` to `muted`, then merge
- *These issues should not be fixed by this system*: move the file to `_declined/`, set `status: declined`, add a `## Why declined` section, then merge

Decline suggested classifications **by setting the status and moving to declined folder**, not by closing the PR or removing the file. The classification directory is ground truth. Closing is not a signal — the class will be proposed again next run. 

Engineers or their local Devin agents can add classifications manually as well and they will be handled the same as the scanner's generated files. 

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

---

### Reviewing Remediation pull requests

Proposed remediation PRs can be reviewed and merged like normal PRs. 

If a PR is proposed for an issue you don't want fixed, label the issue `devin:rejected`, and the automation will closes the PR, deletes the branch, closes the issue, and counts it as rejected on the metrics board 

If you want to re-run the full remediation automation on a PR, remove and re-apply the `devin:ready` label on the issue 

---
