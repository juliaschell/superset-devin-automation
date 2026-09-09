<!--
 Licensed to the Apache Software Foundation (ASF) under one
 or more contributor license agreements.  See the NOTICE file
 distributed with this work for additional information
 regarding copyright ownership.  The ASF licenses this file
 to you under the Apache License, Version 2.0 (the
 "License"); you may not use this file except in compliance
 with the License.  You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing,
 software distributed under the License is distributed on an
 "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 KIND, either express or implied.  See the License for the
 specific language governing permissions and limitations
 under the License.
-->
# Classification registry

Each markdown file in this directory defines one **class of problem** that the
nightly automation may detect and fix in this repository. The registry is the
control surface: it is how a human changes what counts as a defect, how it
should be fixed, and — most importantly — what proves a fix is correct.

Classes arrive as proposals from the scan; the registry started empty.

## Lifecycle

```
scan finds something that fits no class
        │
        ▼
  a PR adding <slug>.md, status: active   ← the proposal, written to be merged as-is
        │
        ├── merge it              ← adopted: the scan looks for it, the remediator fixes it
        │        │
        │   status: muted         ← one-line edit; stops being detected, history is kept
        │
        └── move it to _declined/, status: declined, add ## Why declined
                 ▼
           _declined/<slug>.md    ← not detected, and never proposed again
```

The open PR *is* the proposal, so nothing unadopted sits in this directory. The
scan writes each proposed class as the version it would want merged unchanged —
complete frontmatter, defensible severity, a `max_open` sized so the class drips
rather than floods, and a `validate:` command that runs here. Agreeing with a
proposal should cost a merge; disagreeing should cost one small edit.

Editing a proposal before merging is always available, but it is the exception.
If proposals routinely need rewriting before they are safe to adopt, that is a
bug in the scan prompt, not a step in this process.

## Declining a class

Do it **on the proposal PR**: move the file to `_declined/`, set `status:
declined`, add a `## Why declined` section, and merge that. One edit and the
merge you were making anyway. The scan reads the slugs in `_declined/` before it
surveys and does not propose them again.

Closing the PR instead is not a decline. This directory is the whole record of
what the automation may do here — a reader looking at nothing but these files
must be able to say what is active, what is paused, and what was rejected and
why, and PR history is not something a reader has. So the scan takes no signal
from a closed PR: it will propose the class again, correctly, because nothing
was written down.

Deleting the file instead is the thing to avoid: the next run rediscovers the
same finding, proposes the same class, and nothing records that someone already
considered and rejected it. A decline is a decision, and decisions are worth
keeping.

`_declined/` and `muted` are not the same thing. Declined means never adopted.
Muted means adopted, acted on, and now paused — its issues and PRs are part of
the history.

## Format

```markdown
---
slug: transitive-npm-advisory
status: active          # active | muted | declined
severity: high          # critical | high | medium | low
max_open: 3             # cap on simultaneously open issues of this class
validate: cd superset-frontend && npm audit --audit-level=high
---

# One-line title

One paragraph. What the problem is and why it matters *in this repository's
terms* — cite `AGENTS.md`, `SECURITY.md`, `.cursor/rules/`, or the advisory.

## Recognise

What to run or read, what a genuine instance looks like as opposed to a false
positive, and an explicit exclusions list. Scope matters: a finding must be
fixable by one focused PR, so "237 files match" is a class, not a finding.

## Fix

The approach, and the constraints that apply here — the repo's conventions,
what must not change, when to stop and ask instead of guessing.

## Validate

What the `validate:` command proves and what it does not. If the command is
weak, say so here rather than letting a passing run imply more than it should.

## Seed finding (date)

The instances that motivated the class, with file and line. This is what makes
a proposal reviewable: a class nobody can find an example of should not be
adopted.
```

## The `validate` line

This is the load-bearing field. Every fix in the class is held to it, and the
dashboard reports any run whose actual command differed from it.

- It lives in the frontmatter, on one line, because it is compared
  mechanically against what the session actually ran. `<file>` and `<scope>`
  are substituted with the issue's paths. The `## Validate` section is prose
  about that command, not a second source of truth.
- It must be a command a Devin session can run in this repo, non-interactively.
- It must actually be capable of failing on a bad fix. `echo ok` and
  `npm run build` on an unrelated package are the failure mode to avoid.
- Prefer the repo's own tooling — `pytest`, `npm run type`, `npm audit`,
  targeted `mypy` or `eslint` — over anything bespoke.
- Not `pre-commit run`. Hook environments are installed by cloning from
  github.com, which the remediation sandbox's git proxy refuses, so a
  `pre-commit` gate fails for a reason that has nothing to do with the fix.
  Call the underlying tool directly instead.

A weak validation command is worse than no automation: it makes a broken fix
look verified.

## Notes

- `max_open` bounds review load. The scan counts existing open issues of the
  class and skips it once it is at the cap.
- Muting a noisy class is a one-line edit here — never a code change.
- No pull request produced by this system is ever merged automatically,
  regardless of what a validation command reports.
