# What this system asks of the Devin API, and what it found there

Every claim below was checked against the live API with the org's service-user
key, not read off the docs and assumed. Where the platform behaved differently
from the documentation, the measured behaviour is what the code follows and the
difference is written down here rather than worked around silently.

## Used

| Surface | Endpoint | Why |
| --- | --- | --- |
| Automations | `GET/POST/PATCH /v3/organizations/{org}/automations` | Both automations are created and updated from the checked-in JSON, so the platform's state has a diff and a history rather than a click trail. |
| Automation schemas | `GET .../automations/schemas` | `make validate` checks every trigger against the platform's own catalogue. A trigger naming an event type or filter field that does not exist never fires and reports no error anywhere, so this is the one class of mistake worth a pre-flight check. |
| Playbooks | `GET/POST/PUT .../playbooks` | The remediation procedure lives in a playbook; the automation prompt is only the binding. |
| Sessions | `GET .../sessions`, `.../sessions/{id}` | The reconciler reads state by tag (`superset-remediation`) rather than tracking sessions it started, so a restart loses nothing. |
| Session messages | `GET .../sessions/{id}/messages` | Where a session's report is actually readable — see below. |
| Session tags | `PUT .../sessions/{id}/tags` | Retagging a session out of the run (used to drop a probe session from the reconciler's view). |
| Consumption | `GET .../consumption/daily/sessions/{id}` | Per-session cost, when the plan exposes it. |
| Webhook trigger | issued per automation | The manual "run now" path. |

## Measured platform behaviour that shaped the design

- **`structured_output` is null on automation-spawned sessions**, and the
  automations API has no field to attach a schema to one: `session.secret_ids`,
  `session.knowledge_ids` and `session.structured_output_schema` are all
  rejected as extra inputs. A playbook's own `structured_output_schema` does not
  reach a session either — a probe session that arrived through a playbook token
  reported no schema attached. So the schema is appended to the prompt and the
  shape is *requested, not enforced*: the reconciler treats every field as
  optional and reads the report out of the session's final message.
- **`session.playbook_id` is read-only** and derives from an `@playbook:<id>`
  token in the prompt. The id, not the title or the macro — both of those are
  rejected. `scripts/apply_playbooks.py` creates the playbook, and
  `scripts/apply_automations.py` looks its id up by title and prepends the
  token, so no platform identifier is checked into the repo.
- **Playbook text does reach the session.** The probe quoted the first bound
  from the playbook back verbatim, which is the check worth doing before moving
  a session's limits out of its prompt.
- **The consumption API is Enterprise-only.** On this plan the org, session and
  user endpoints all return `total_acus: 0.0` with an empty series, and the
  enterprise-scoped endpoint 403s. Empty is read as unknown, not as free: the
  cost panel, the report section and the ACU metric all disappear rather than
  showing a zero. On an Enterprise plan the same code path reports real numbers.
- **`acus_consumed` on the session object is 0.0** even for sessions that did
  hours of work, which is why cost comes from the consumption endpoint instead.
- **`/v1/sessions` rejects a service key** (401). It is the personal surface;
  everything here goes through `/v3/organizations/{org}/...`.
- **There is no run-now endpoint.** A manual scan is an inbound-webhook trigger,
  whose secret the API returns as `null` — it is readable only in the UI.
- **Automation email notifications are not available to service-user
  automations**, so run visibility is the dashboard, not mail.

## Deliberately not used

- **Scratchpad** (automation memory across runs). The registry is the memory,
  and the point of it is that a human can read and edit it in a diff. A
  scratchpad would be a second, invisible store to disagree with the first.
- **Enterprise endpoints** (`/v3/enterprise/...`, audit logs, org listing) — not
  available on this plan, and none of them is load-bearing here.
- **Knowledge and secrets APIs.** The org has secrets, and sessions inherit
  them; narrowing that per automation is not offered on the automations API
  (`secret_ids` is rejected). Worth revisiting when it is, because a remediation
  session needs one credential and currently could see more.
- **`/v3/enterprise/consumption/...`**, which 403s here.
