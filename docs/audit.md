# Four reviews of this system

Written as four readers who would each reject it for a different reason. Each
finding says what was changed, or why it was accepted as-is. Findings that were
accepted are the interesting ones — they are the limits of what this build
claims.

---

## 1. The VP of Engineering: does this pay for itself?

**Q. What is the unit economics?**
Unanswerable on this account, and the report says so rather than guessing. Cost
per merged PR needs per-session ACUs, which come from the consumption API, which
is Enterprise-only; below that plan it authorizes and returns an empty series.
The code path is there and reports measured spend unchanged on an Enterprise
org. Where nothing is measured, no cost is shown anywhere — no dashboard panel,
no report section, no `/metrics` series. **Accepted:** the honest answer to "what
did it cost" here is "this plan does not expose it", and a number typed off a
usage page would sit in the report indistinguishable from a measured one.

**Q. Success rate over what denominator?**
Verified PRs over *settled* tasks; in-flight work is excluded rather than counted
as failure. Success requires the classification's validation command to have
passed — a session that finished on a red build is not a success. Autonomy is
measured over everything that settled *or* needed a human, because over successes
alone it reads ~100% and means nothing.

**Q. Isn't "human rejected it" just failure with better PR?**
No, and separating them is the point. Rejection judges the *scanner* — we fixed
something nobody wanted, and the correction is a registry edit. Failure judges
the *fixer*. Same count, opposite fixes.

**Q. Sample size?**
Small, and printed at the top of the report above every rate. Nothing here
claims a rate is stable at this volume.

---

## 2. The security reviewer: it reads a public repo and writes code

**Q. Prompt injection. The fork is public, anyone can comment on an issue, and
that text goes into a prompt.**
It does, and it is treated as untrusted throughout. The controls are layered so
that a successful injection still cannot do much:

- **Egress is an allowlist** (`net_policy`): git, npm, PyPI. A hijacked session
  has nowhere to exfiltrate to.
- **No merge capability** anywhere — not in the prompt, not in the client. A test
  asserts the GitHub client contains no merge call.
- **The label trigger needs write access.** Commenting does not, which is why the
  trigger is a label and not a `/devin` comment on a public repo.
- **ACU and invocation caps** per automation bound one bad session and bound
  trigger spam.
- Sessions run as a **scoped service identity**, not anyone's personal account.

**Q. So the worst case is…**
A session persuaded to open a bad PR. That is the residual risk, and it is
deliberately where the risk was pushed: a PR is inert until a human merges it,
and no path to merging exists. **Accepted, and it is the design.**

**Q. The validation command comes from a file an agent proposed. Is that a gate
or a rubber stamp?**
It is a gate only to the extent a human read it. Proposals arrive merge-ready
with a real command, which is convenient and is also exactly the risk: merging
one unread adopts a validation command nobody checked. Two things reduce it —
the command is in a reviewable diff rather than improvised per-run, and the
dashboard counts every run whose actual command differed from the registry's.
**Accepted with mitigation:** the registry is only as good as its review, and the
README says so.

**Q. Secrets.**
Nothing but env vars; `.env` is gitignored and the recorded run committed for
the offline demo was slimmed to the fields replay uses and scanned for
credential patterns before commit.

---

## 3. The SRE: what happens at 3am when it breaks?

**Q. The poller dies. How do I find out?**
`/healthz` returns 503 once reconciliation is older than four poll intervals, and
the dashboard prints its own staleness in the header. A dashboard that has
quietly stopped updating is worse than one that is down.

**Q. A cycle throws.**
It is caught per phase, recorded as an `error` event, and the loop continues.
One broken phase does not stop the other two, and a failing cycle cannot kill
the process.

**Q. The Devin API is slow rather than down.**
Transport errors are translated into the same error type as HTTP errors and
retried once, so a slow request degrades that one reading — a session whose
report we could not fetch this cycle — instead of aborting the pass. This was a
real bug: `httpx.ReadTimeout` escaped the handlers meant to tolerate it and
killed the whole sessions phase.

**Q. State after a restart.**
SQLite on a volume, and every field in `tasks` is re-derivable from Devin and
GitHub. Losing the file loses history, not correctness. Attempts are counted
from distinct session IDs in the append-only log, so recounting after a restart
gives the same answer.

**Q. Rate limits.**
Steady state is one Devin list call plus one GitHub call per in-flight task per
30s cycle. Merged PRs are terminal and are not re-fetched, so settled work stops
costing calls. At a handful of findings a night this is far below GitHub's
authenticated budget.

**Q. Stuck sessions.**
Recorded as `timed_out` against a configurable age, distinct from
`blocked_on_human` (waiting on a person) and `budget_exhausted` (ran out of
credits, never got a fair attempt). Failure taxonomy is separated because those
three call for different responses.

**Q. What is missing?**
No alerting — `/metrics` is exposed but nothing pages. No durable retry across
process restarts; retry here means a human corrected the problem. **Accepted:**
both are graduation items, and the README names the trigger for each.

---

## 4. The Superset maintainer: I did not ask for these PRs

**Q. Is this a spam machine pointed at my repo?**
Not at yours. Triggers are scoped to one fork on purpose, and the registry lives
in the repo it describes. Nothing here is aimed upstream, and multi-repo support
is deliberately absent.

**Q. If it were, what would stop the flood?**
`max_open` per classification: the class drips rather than floods, and it is a
number in the class file, not in code. Duplicates are skipped against open
issues. A class that keeps producing unwanted work has a visible rejection rate
and is muted with a one-line edit.

**Q. Do the PRs respect the project's own standards?**
That is the strongest evidence in the run: findings were declined for
repo-standards reasons — a dependency with no fixed release, an antd import with
no core re-export, DoS-only advisories that `SECURITY.md` puts out of scope — and
a fix was skipped because the only remedy was a version the project's own
`pyproject.toml` forbids. Declining work it cannot complete correctly is more
maintainer-friendly than filing it.

**Q. Security findings?**
`SECURITY.md` is authoritative, and findings must name the role/capability row
violated and the attacker principal, or be filed as questions rather than
vulnerabilities. That requirement is the repo's, and the scan prompt enforces it.

**Q. Would I want this on my project?**
Honest answer: only with the registry curated by maintainers, and only into a
fork first. The value is not "an agent fixes your repo" — it is that what counts
as a problem, and what counts as proof of a fix, becomes a reviewable file that
a maintainer owns.
