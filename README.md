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

The tracker polls Devin and GitHub, writes down what it sees, and serves the
dashboard, `/metrics` and `report.md`.

### What is Devin's and what is ours

Anything the platform does natively, we do not build. The boundary is the
interesting part of this design, so it is explicit:

| Concern | Owner | Why |
|---|---|---|
| Nightly schedule | Devin Automation (`schedule:recurring`) | native trigger |
| Manual scan run | Devin Automation `github:issues` trigger on a `devin:scan` label | there is no run-now endpoint, and the webhook trigger's secret is issued once in the UI; a label is the manual entry point the same GitHub token can already reach |
| GitHub event matching | Devin Automation triggers | `github:issues` + `github:pull_request_review`, with a conditions DSL |
| Session dispatch | Devin Automation `start_session` | no webhook receiver, no HMAC, no dispatch loop |
| Replying on the issue/PR | Devin `post_response` | no status-comment code of ours |
| Budget, concurrency, egress | Devin `limits` / `concurrency` / `net_policy` | native guardrails |
| The remediation procedure | Devin playbook, referenced by the automation prompt | the prompt binds a repo and a trigger; the playbook says how the work is done, and is versioned here as `remediator/playbook.md` |
| Result shape | requested in the prompt | the API rejects a schema on a spawned session, so it is advisory — every structured field is optional to the tracker, and a missing one is recorded, not assumed |
| Correlating attempts to an issue | **ours** | |
| Longitudinal outcomes, cost, funnel | **ours** | Automations record *invocations*; the question here is *outcomes* |
| Cleanup of rejected work | **ours** | |

Where the platform behaved differently from its documentation, the measured
behaviour and its consequence are written down beside the code that works
around it, in `shared/devin.py`.

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
  _declined/test-describe-nesting.md   ← considered and rejected, with reasons
```

Each file states what the class is, how to recognise it, how to fix it, the
**command that validates a fix**, a severity, `max_open`, and whether it is
active. The scan looks for instances of active classes *and* for things that fit
no class — the latter arrive as a PR adding the class file, written to be merged
unchanged: complete frontmatter, `status: active`, a `max_open` sized so the
class drips rather than floods. Agreeing costs a merge. Disagreeing costs one
edit — `status: muted`, or a move to `_declined/` with a `## Why declined` note,
which the scan reads and never proposes again. Deleting it instead would bring
the same proposal back the next night with the decision lost.

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

There is one mode: live, against your own fork and your own Devin org. Two
commands, whether you run them or your Devin does.

```bash
export REPO=you/superset
export DEVIN_API_KEY=...   # service user, Admin role
export DEVIN_ORG_ID=...
export GITHUB_TOKEN=...
make up                    # http://localhost:8000
```

There is no file to copy or fill in: the four values are read from the shell and
passed through to the container. They are exported rather than given as
`make up REPO=...` because three of the four are secrets, and a command line
ends up in shell history and in `ps`.

`make up` bootstraps before it serves: it forks Superset if `REPO`
does not exist yet, enables Issues, creates the labels, seeds an empty
classification registry, and creates the playbook and both automations over
REST, scoped to your fork. All of it is idempotent, so it runs on every start
and a second run only prints what it found.

Then `make scan` files a `devin:scan` issue to trigger the first run instead of
waiting for 02:00 PT. The first scan finds no active classes and proposes some
as a PR — merging it is what turns detection on.

### Or point your own Devin at it

Give Devin the repo and this prompt; [`AGENTS.md`](AGENTS.md) tells it the rest.

> Set up https://github.com/juliaschell/superset-devin-automation against a
> fresh fork of Apache Superset in my org, run it, and show me the dashboard.
> Follow its AGENTS.md. Never merge anything.

It will ask for the same four values, which are the only inputs either path has.

### What you have to grant

- A **service-user** API key with the Admin role. The `/v3/organizations/*`
  endpoints are service-user RBAC — a personal key is rejected there:
  https://app.devin.ai/settings/org-service-users
- A **GitHub token** with `repo`, issues and pull-request write. Not merge.
- **Devin's GitHub connection**, scoped to all installed repos, so label and
  review events on a public fork reach the automations. This is the one step
  no API exposes; bootstrap prints the link when it finishes.

Without Docker: `make install && make bootstrap && make run`. Every Make target
says who it is for in the comment above it.

### Endpoints

| Path | |
|---|---|
| `/` | dashboard |
| `/metrics` | Prometheus |
| `/metrics.json` | same numbers as JSON |
| `/report.md` | the honest write-up, methodology first |
| `/healthz` | 503 if the watch loop has gone stale |

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
- **Durations come from GitHub's timestamps**, not from this process's clock:
  when the issue was filed and when the PR was opened or merged. A tracker
  started after the work — which is what happens on a fresh clone — would
  otherwise report a day-long cycle as the few seconds between its own first two
  observations.
- **Cost is measured or it is absent — never typed in, never zero.** Run spend
  is fetched per session from
  `GET /v3/organizations/{org}/consumption/daily/sessions/{session_id}` — the
  endpoint the usage dashboard is built on — not from the session object's
  `acus_consumed` field, which reads `0.0` for every session observed here.
  On a Teams org that call authorizes and returns `total_acus: 0.0` with an empty
  `consumption_by_date`, because **the consumption API is documented as
  Enterprise-only**: Teams and self-serve accounts get no rows, and the
  enterprise-scoped variant `403`s. That is a plan boundary, not a defect and
  not a zero. An empty series is treated as unmeasured, and unmeasured cost is
  not reported at all: the ACU rows vanish from the dashboard, the report has no
  Cost section, and `/metrics` emits no ACU series. On an Enterprise account the
  same code path reports measured per-session cost with no changes. There is
  deliberately no `RUN_ACUS` knob — a figure read off a usage page by hand is a
  claim about spend this system did not measure, and would sit in the report
  indistinguishable from one it did.

No outcome is fabricated in either direction — no engineered failures, no
flattering denominators. Sample size is printed above every rate in the report.

---

## Sizing, and when to graduate

This is deliberately small: steady state is a nightly scan producing a handful of
findings. One container, one process, one SQLite file. The upgrade paths are
cheap, so they are not pre-built:

| Move to… | When |
|---|---|
| Postgres | trackers span more than one host, or sustained concurrency exceeds ~10 sessions |
| A separate worker process | dashboard latency is affected by the poll loop |
| A workflow engine (Temporal et al.) | steps need durable retries across process restarts, or the state machine exceeds ~10 states |
| Event sourcing (drop the `tasks` table) | you need to reconstruct *system state* at a past time, not just events |
| A BI tool over the SQLite file | anyone asks a question the fixed dashboard cannot answer |
| Devin native code scans | detection needs to run across many repos rather than one |

### The two tables

`task_events` is append-only and justified because the data **exists nowhere
else**: transitions, our own decisions, and the timings every metric derives
from. `tasks` is a cache of the latest state — every field re-derivable from
Devin and GitHub — and earns its place on read cost, dedup, and our own
annotations (rejection, cleanup, attempt chains). Delete the file and you lose
history, not correctness.

Devin is authoritative for session and PR state; we overwrite rather than merge,
so drift is *staleness*, never a conflict. The dashboard prints how stale it is,
and `/healthz` fails when the loop stops — a dashboard that has quietly stopped
updating is worse than one that is down.

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

One directory per system. The two Devin-side systems are definitions rather than
code we run — the platform runs them — so each holds its own automation spec,
the prompt it runs, and the output shape it is asked for.

```
scanner/              finds problems, files issues, proposes new classes
  automation.json     nightly schedule + the devin:scan label
  prompt.md           the prose, reviewable as prose in a diff
  output_schema.json  the result shape, requested in the prompt
  registry_seed.md    the registry format, seeded into a fresh fork
  run_now.py          fire a scan now, by labelling an issue
remediator/           fixes them, validates the fix, opens a PR
  automation.json     devin:ready label + changes_requested review
  prompt.md
  output_schema.json
  playbook.md         the remediation procedure, held by the platform
tracker/              the only code that runs here: watch, record, report
  watch.py            what each observation means; pure functions, testable
  metrics.py          every definition above, in code
  store.py            two tables, and why each exists
  app.py, templates/  the dashboard and its endpoints
shared/               clients and config both sides use
  devin.py, github.py, config.py
bootstrap/            one idempotent pass from nothing to a running system
  __main__.py         fork setup, then the playbook and both automations
  playbooks.py        create/update the playbooks over REST
  automations.py      validate (--check) or create/update over REST
docker/               Dockerfile + compose.yml, out of the way of the code
tests/                including one that asserts we cannot merge
```

Metrics have no directory of their own: they are the tracker's reason for
existing rather than a system beside it.

Dependencies are declared once, in `pyproject.toml` — the runtime four, plus a
`[dev]` extra for test and lint tooling that never ships in the image.

```bash
make check    # ruff + mypy + pytest
```
