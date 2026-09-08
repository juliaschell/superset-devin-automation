# Superset remediation — a Devin-driven detect → fix → verify loop

A nightly automation that finds real problems in a fork of
[Apache Superset](https://github.com/apache/superset), fixes them, proves the
fix, and opens a pull request for a human to judge.

Two Devin Automations do the work. A small service — one process, one container,
one SQLite file — watches what happened and reports on it honestly.

**No pull request is ever merged automatically.** That is the one hard
invariant; everything else in here is a tunable.

---

## How it works

```
                      ┌──────────────── nightly (or `make scan`) ───────────────┐
                      ▼                                                          │
              ┌───────────────┐   files issues, labels them   ┌──────────────┐   │
   fork ─────▶│ scan session  │──────────  devin:ready  ──────▶│    GitHub    │   │
              └───────────────┘                                └──────┬───────┘   │
                      │ proposes new classes as a PR                  │           │
                      ▼                                    label │    │ review    │
           .devin/classifications/        ┌─────────────────────┘    │ changes    │
             (human-curated)              ▼                          ▼ requested  │
                      ▲            ┌────────────────┐                            │
                      └── read by ─│ remediate      │◀───────────────────────────┘
                                   │ session        │
                                   └───────┬────────┘
                                           │ fix + run the class's validate command
                                           ▼
                                     PR (never merged)
                                           │
                    ┌──────────────────────┴───────────────────────┐
                    ▼                                              ▼
             human merges                             human requests changes
                                                       → same branch, attempt 2
```

The control plane polls Devin and GitHub, writes what it sees to SQLite, and
serves the dashboard, `/metrics`, and `report.md`.

### What is Devin's and what is ours

Anything the platform does natively, we do not build. The boundary is the
interesting part of this design, so it is explicit:

| Concern | Owner | Why |
|---|---|---|
| Nightly schedule | Devin Automation (`schedule:recurring`) | native trigger |
| Manual scan run | Devin Automation `webhook:incoming` trigger | there is no run-now endpoint, so the manual entry point is a second trigger rather than a button of ours |
| GitHub event matching | Devin Automation triggers | `github:issues` + `github:pull_request_review`, with a conditions DSL |
| Session dispatch | Devin Automation `start_session` | no webhook receiver, no HMAC, no dispatch loop |
| Replying on the issue/PR | Devin `post_response` | no status-comment code of ours |
| Budget, concurrency, egress | Devin `limits` / `concurrency` / `net_policy` | native guardrails |
| Result shape | requested in the prompt | the API rejects a schema on a spawned session, so it is advisory — every structured field is optional to the reconciler, and a missing one is recorded, not assumed |
| Correlating attempts to an issue | **ours** | |
| Longitudinal outcomes, cost, funnel | **ours** | Automations record *invocations*; the question here is *outcomes* |
| Cleanup of rejected work | **ours** | |

That last block is the whole reason this repo exists. The Activity tab answers
"did it fire". It cannot answer "did the fix hold, how long did it take, what
did it cost, and where does work die."

---

## The classification registry — the human control surface

Issue classes are **not** hardcoded here. They live as markdown in the fork:

```
.devin/classifications/
  transitive-npm-advisory.md
  stale-python-lockfile.md
  _proposed/deprecated-flask-api.md   ← proposed by a scan, not yet adopted
```

Each file states what the class is, how to recognise it, how to fix it, the
**command that validates a fix**, a severity, `max_open`, and whether it is
active. The scan looks for instances of active classes *and* for things that fit
no class — the latter arrive as a PR into `_proposed/`, which a human edits and
adopts. Muting a noisy class is a one-line edit, not a code change.

This is what makes the verification credible. A generic remediator that picks
its own validation command can pick a weak one; here the gate is curated,
version-controlled, and reviewable, and the dashboard flags any run whose actual
command differed from the registry's.

---

## Human control

| You want to… | You do… |
|---|---|
| Add work by hand | Open an issue, label it `devin:ready` |
| Re-run on a corrected problem | Edit the issue, remove and re-apply the label |
| Ask for changes on a PR | Request changes — that alone starts attempt 2 **on the same branch** |
| Reject a finding | Label the issue `devin:rejected`; the loop closes the PR, deletes the branch, closes the issue, and counts it as *rejected* — not as a failure |
| Change how a class is fixed or validated | Edit its markdown file |
| Stop a class entirely | Set `status: muted` |
| Merge | Yourself. Always. |

---

## Running it

### Offline, no credentials

```bash
make install
make demo        # http://localhost:8000
```

Replays a recorded real run — the actual issues filed and the actual session
objects returned — through the same reconciler and metrics code that runs live.
Nothing offline is synthetic; see [`demo/README.md`](demo/README.md).

### Live

```bash
cp .env.example .env    # fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN
make validate           # check both payloads against the platform's trigger catalogue
make apply              # create/update both automations from automations/*.json
SCAN_WEBHOOK_SECRET=... make scan   # fire the scan now instead of waiting for 02:00
MODE=live make run      # dashboard + reconcile loop
```

Or `docker compose up --build`.

Prerequisites, all one-time:

- A **service-user** API key. The `/v3/organizations/*` endpoints are
  service-user RBAC — a personal key is rejected there. Needs
  `ViewOrgAutomations`, `ManageOrgAutomations`, and `ViewOrgSessions`.
- Devin's GitHub connection scoped to **All installed repos** — GitHub triggers
  only fire on private repos by default, and the fork is public.
- **Issues enabled** on the fork (GitHub disables them on forks).
- The scan automation's **webhook secret**, for manual runs. The API returns it
  as `null`, so it is copied once from the automation's page.

### Endpoints

| Path | |
|---|---|
| `/` | dashboard |
| `/metrics` | Prometheus |
| `/metrics.json` | same numbers as JSON |
| `/report.md` | the honest write-up, methodology first |
| `/healthz` | 503 if reconciliation has gone stale |

---

## What the numbers mean

Definitions are choices, so they are stated rather than implied:

- **Success** = a PR whose classification's validation command *passed*. A
  session that finished on a red build is not a success.
- **Autonomy** is measured over everything that settled *or* needed a human. Over
  successes alone it reads ~100% and means nothing — the blocked session, which
  is the clearest possible loss of autonomy, would not be in the denominator.
- **Rejection is not failure.** Rejection judges the *scanner* — we fixed
  something nobody wanted. Failure judges the *fixer*. They are counted
  separately because they call for different corrections.
- **The funnel is cumulative** ("ever reached"), not current state. Current state
  renders a completed run as a row of zeroes.
- **Merged** counts human decisions only. Nothing here can merge.
- **Rates are `null`, not `0`, when there is no data.** Zero reads as a
  measurement of failure.
- **Run ACUs are measured, build ACUs are declared.** The v3 session object
  reports `acus_consumed`, so run spend is summed from the sessions that did the
  work. Build spend — the planning and implementation sessions that produced
  this system — carries no such tag, so it is a configured figure and the
  dashboard labels which is which. The payback question is about both.

No outcome is fabricated in either direction — no engineered failures, no
flattering denominators. Sample size is printed above every rate in the report.

---

## Sizing, and when to graduate

This is deliberately small: steady state is a nightly scan producing a handful of
findings. One container, one process, one SQLite file. The upgrade paths are
cheap, so they are not pre-built:

| Move to… | When |
|---|---|
| Postgres | reconcilers span more than one host, or sustained concurrency exceeds ~10 sessions |
| A separate worker process | dashboard latency is affected by the poll loop |
| A workflow engine (Temporal et al.) | steps need durable retries across process restarts, or the state machine exceeds ~10 states |
| Event sourcing (drop the `tasks` table) | you need to reconstruct *system state* at a past time, not just events |
| A BI tool over the SQLite file | anyone asks a question the fixed dashboard cannot answer |
| Devin native code scans | detection needs to run across many repos rather than one |

### The two tables

`task_events` is append-only and justified because the data **exists nowhere
else**: transitions, our own decisions, and the timings every metric derives
from. `tasks` is a materialized view — every field re-derivable from Devin and
GitHub — and earns its place only on read cost, dedup, and our own annotations
(rejection, cleanup, attempt chains). Delete the file and you lose history, not
correctness.

Devin is authoritative for session and PR state; we overwrite rather than merge,
so drift is *staleness*, never a conflict. The dashboard prints how stale it is,
and `/healthz` fails when reconciliation stops — a dashboard that has quietly
stopped updating is worse than one that is down.

---

## Security posture

The fork is public, so **issue bodies, comments, and PR reviews are untrusted
input** that flows into a prompt. The controls:

- **Egress is an allowlist** (`net_policy`). A hijacked session can reach git,
  npm and PyPI, and nothing else — there is nowhere to send anything.
- **No merge capability**, in the prompt and in the token scope. A test asserts
  the GitHub client contains no merge call.
- **`max_acu_limit` and invocation caps** per automation bound the blast radius
  of a session that goes wrong, or of someone spamming the trigger.
- **The label trigger requires write access** to apply, which the fork's
  collaborators have and the public does not. Comments would not.
- Sessions run as a **scoped service identity**, not a personal account.

---

## What is not built, and why

- **No auto-merge, and no path to one.** Requested constraint, and the right one.
- **No webhook receiver.** Automations trigger natively; a receiver would be a
  second thing to secure and operate.
- **No queue.** Concurrency and queue depth are automation settings.
- **No retry-with-backoff engine.** Retry here means "a human corrected the
  problem", which is a review or a label, not a timer.
- **No multi-repo support.** Triggers are scoped to one fork on purpose; the
  registry lives in the repo it describes.

---

## Layout

```
automations/          checked-in automation definitions — the source of truth
  scan.json           nightly schedule
  remediate.json      devin:ready label + changes_requested review
  prompts/            the prose, reviewable as prose in a diff
  schemas/            structured-output contracts
scripts/
  apply_automations.py   validate (--check) or create/update over REST
  run_scan.py            fire the scan now, natively
  record_run.py          capture a live run for offline replay
src/
  reconcile.py        the only logic that matters; pure functions, testable
  metrics.py          every definition above, in code
  store.py            two tables, and why each exists
  replay.py           offline mode
tests/                including one that asserts we cannot merge
```

```bash
make check    # ruff + mypy + pytest
```
