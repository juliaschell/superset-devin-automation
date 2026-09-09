"""Reconciliation: the only thing this service actually does.

Automations start every session; we never dispatch. This loop observes what
happened, writes it down, and cleans up work a human rejected.

The interesting logic is in the pure functions at the top — they take an
observation and return what should change — so the behaviour is testable
without a network.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .github import pr_number_from_url
from .store import Store

# The v3 lifecycle is two fields, not one. ``status`` is coarse
# (new/claimed/running/exit/error/suspended/resuming) and a session that has
# *completed its task* is still ``running`` — with ``status_detail == finished``.
# Reading status alone would leave every completed session counted as in flight.
TERMINAL_STATUSES = {"exit", "error", "suspended"}
FINISHED_DETAIL = "finished"
# Devin is waiting on a person: the clearest possible loss of autonomy.
BLOCKED_DETAILS = {"waiting_for_user", "waiting_for_approval"}
# Suspension reasons that mean we ran out of budget rather than out of work.
BUDGET_DETAILS = {
    "usage_limit_exceeded",
    "out_of_credits",
    "out_of_quota",
    "no_quota_allocation",
    "payment_declined",
    "org_usage_limit_exceeded",
    "user_usage_limit_exceeded",
    "total_session_limit_exceeded",
}

OUTCOME_TO_STAGE = {
    "pr_opened_validated": "verified",
    "pr_opened_validation_failed": "pr_open",
}

FAILURE_REASONS = {
    "no_matching_class": "no_matching_class",
    "blocked": "blocked_on_human",
    "abandoned": "abandoned",
    "pr_opened_validation_failed": "verification_failed",
}


def _status(session: dict[str, Any]) -> tuple[str, str]:
    return (
        str(session.get("status") or ""),
        str(session.get("status_detail") or ""),
    )


def is_done(session: dict[str, Any]) -> bool:
    status, detail = _status(session)
    return status in TERMINAL_STATUSES or detail == FINISHED_DETAIL


def is_blocked(session: dict[str, Any]) -> bool:
    return _status(session)[1] in BLOCKED_DETAILS


def _started_at(session: dict[str, Any]) -> float | None:
    """Epoch seconds for ``created_at``, which the API sends as an ISO string.

    Treating it as a number silently disables timeout detection, so an
    unparseable value returns None rather than a zero that ages instantly.
    """
    started = session.get("created_at")
    if isinstance(started, int | float):
        return float(started)
    if isinstance(started, str):
        try:
            return datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def structured(session: dict[str, Any]) -> dict[str, Any]:
    """The session's own report of what it did.

    Read from ``structured_output`` — which the platform leaves null on every
    automation-spawned session, because a schema cannot be attached to one. The
    reconciler fills the field in from the session's final message before these
    functions see it, so they stay pure and there is one place to read.
    """
    out = session.get("structured_output")
    return out if isinstance(out, dict) else {}


def issue_number_for(session: dict[str, Any]) -> int | None:
    """Which issue a session is working on.

    Structured output is authoritative. Before it exists, a session carries an
    ``issue-<n>`` tag only if something set one, so early in a session's life we
    may legitimately not know — that is why unattached sessions are logged
    rather than dropped.
    """
    value = structured(session).get("issue_number")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    for tag in session.get("tags") or []:
        if tag.startswith("issue-") and tag[6:].isdigit():
            return int(tag[6:])
    return None


def pr_url_for(session: dict[str, Any]) -> str | None:
    out = structured(session)
    if isinstance(out.get("pr_url"), str) and out["pr_url"]:
        return out["pr_url"]
    # The API's own view of the session's PRs, which beats the prompt-requested
    # field when a session opened one but never reported it.
    for pr in session.get("pull_requests") or []:
        if isinstance(pr, str) and pr:
            return pr
        if isinstance(pr, dict):
            url = pr.get("url") or pr.get("pr_url")
            if url:
                return str(url)
    return None


def task_update_from_session(session: dict[str, Any]) -> dict[str, Any]:
    """Everything we learn about a task from one session object."""
    out = structured(session)
    status, detail = _status(session)
    update: dict[str, Any] = {
        "session_id": session.get("session_id"),
        # Both halves, because "running" alone hides the difference between
        # working, waiting on a human, and done.
        "session_status": f"{status}/{detail}" if detail else status,
        "classification": out.get("classification"),
        "validate_command": out.get("validate_command"),
        "validate_registry": out.get("validate_command_from_registry"),
        "outcome": out.get("outcome"),
        "pr_url": pr_url_for(session),
        # Measured, not configured: the run-cost half of the payback figure is
        # whatever the sessions actually burned, per the consumption API. The
        # reconciler substitutes that for the session object's own field, which
        # reads zero regardless.
        "acus": session.get("acus_consumed"),
    }
    if "validation_passed" in out:
        update["validation_passed"] = 1 if out.get("validation_passed") else 0
    return {k: v for k, v in update.items() if v is not None}


def stage_for_session(session: dict[str, Any], pr_state: str | None = None) -> str:
    """The furthest stage this session's evidence supports.

    Deliberately conservative: "the session finished" is not success. Only a
    validated PR reaches ``verified``, and only GitHub saying merged reaches
    ``merged`` — which we learn from PR state, never assume.
    """
    out = structured(session)
    outcome = out.get("outcome")
    if pr_state == "merged":
        return "merged"
    if outcome in OUTCOME_TO_STAGE:
        return OUTCOME_TO_STAGE[outcome]
    if pr_url_for(session):
        return "pr_open"
    if is_done(session) or is_blocked(session):
        return "dispatched"
    return "running"


def failure_reason(session: dict[str, Any], timeout_seconds: int, now: float | None = None) -> str | None:
    """Why this task did not produce a verified PR. ``None`` means it did, or
    is still trying."""
    out = structured(session)
    outcome = out.get("outcome")
    if outcome == "pr_opened_validated":
        return None
    if outcome in FAILURE_REASONS:
        return FAILURE_REASONS[outcome]
    status, detail = _status(session)
    if is_blocked(session):
        return "blocked_on_human"
    if status == "error":
        return "session_error"
    # An operational failure, not a remediation one: the work never got a fair
    # attempt, so it should not be filed under "Devin could not fix it".
    if detail in BUDGET_DETAILS:
        return "budget_exhausted"
    if is_done(session) and not out:
        return "no_output"
    started = _started_at(session)
    if not is_done(session) and started is not None:
        if (now or time.time()) - started > timeout_seconds:
            return "timed_out"
    return None


class Reconciler:
    def __init__(self, store: Store, devin: Any, github: Any, config: Any) -> None:
        self.store = store
        self.devin = devin
        self.github = github
        self.config = config

    # ------------------------------------------------------------------ pass

    def cycle(self) -> dict[str, int]:
        """One reconciliation pass. Never raises: a failed cycle records itself
        and the dashboard's freshness stamp is what tells you."""
        stats = {"issues": 0, "sessions": 0, "cleaned": 0, "errors": 0}
        try:
            stats["issues"] = self.sync_issues()
        except Exception as exc:  # noqa: BLE001 - a broken cycle must not kill the loop
            stats["errors"] += 1
            self.store.log("error", detail=f"sync_issues: {exc}")
        try:
            stats["sessions"] = self.sync_sessions()
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            self.store.log("error", detail=f"sync_sessions: {exc}")
        try:
            stats["cleaned"] = self.cleanup_rejected()
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            self.store.log("error", detail=f"cleanup_rejected: {exc}")
        self.store.set_meta("last_reconciled", str(time.time()))
        return stats

    # --------------------------------------------------------------- sources

    def sync_issues(self) -> int:
        """Queued work: issues a scanner or a human labelled."""
        count = 0
        for issue in self.github.issues_with_label(self.config.ready_label):
            if issue.get("pull_request"):
                continue  # GitHub returns PRs from the issues endpoint
            number = issue["number"]
            created = self.store.upsert_task(
                number,
                title=issue.get("title", ""),
                issue_url=issue.get("html_url", ""),
            )
            if created:
                self.store.log(
                    "detected",
                    issue_number=number,
                    detail=issue.get("title", ""),
                    payload={"labels": [label["name"] for label in issue.get("labels", [])]},
                )
                self.store.advance(number, "detected")
            count += 1
        return count

    def sync_sessions(self) -> int:
        """What Devin is doing about it. One call for every in-flight session."""
        sessions = self.devin.list_sessions(tags=[self.config.session_tag])
        for session in sessions:
            session = {
                **session,
                "structured_output": self.devin.report(session),
                "acus_consumed": self.devin.session_acus(str(session.get("session_id") or "")),
            }
            session_id = session.get("session_id")
            number = issue_number_for(session)
            if number is None:
                self.store.log(
                    "session_unattached",
                    session_id=session_id,
                    detail="/".join(p for p in _status(session) if p),
                )
                continue
            task = self.store.get_task(number)
            update = task_update_from_session(session)
            if task and task.get("session_id") and task["session_id"] != session_id:
                # A second session against the same issue is a rework attempt,
                # not a duplicate. Chaining these is what makes attempts-per-issue
                # meaningful.
                update["attempts"] = (task.get("attempts") or 1) + 1
                self.store.log(
                    "attempt", issue_number=number, session_id=session_id, detail="rework"
                )
            elif not task or not task.get("session_id"):
                update["attempts"] = 1
                self.store.log("dispatched", issue_number=number, session_id=session_id)
            self.store.upsert_task(number, **update)

            reason = failure_reason(session, self.config.session_timeout_seconds)
            if reason:
                self.store.upsert_task(number, failure_reason=reason)
            pr_state = self.pr_state(update.get("pr_url") or (task or {}).get("pr_url"))
            if pr_state:
                self.store.upsert_task(number, pr_state=pr_state)
            self.store.advance(number, stage_for_session(session, pr_state))
        return len(sessions)

    def pr_state(self, pr_url: str | None) -> str | None:
        if not pr_url:
            return None
        number = pr_number_from_url(pr_url)
        if number is None:
            return None
        try:
            pr = self.github.get_pull_request(number)
        except Exception as exc:  # noqa: BLE001
            self.store.log("error", detail=f"pr_state {pr_url}: {exc}")
            return None
        if pr.get("merged_at"):
            return "merged"
        return pr.get("state")

    # --------------------------------------------------------------- cleanup

    def cleanup_rejected(self) -> int:
        """A human labelled the issue ``devin:rejected``: close the PR, delete
        the branch, close the issue, and record it as *rejected* — which is a
        different thing from *failed*. Failure is the remediator's; rejection is
        the scanner's, or the class definition's.
        """
        cleaned = 0
        for issue in self.github.issues_with_label(self.config.rejected_label, state="all"):
            number = issue["number"]
            task = self.store.get_task(number)
            if task and task.get("cleaned_up"):
                continue
            self.store.upsert_task(number, rejected=1, outcome="human_rejected")
            pr_url = (task or {}).get("pr_url")
            pr_number = pr_number_from_url(pr_url) if pr_url else None
            if pr_number:
                pr = self.github.get_pull_request(pr_number)
                if pr.get("state") == "open":
                    self.github.close_pull_request(pr_number)
                branch = (pr.get("head") or {}).get("ref")
                if branch:
                    try:
                        self.github.delete_branch(branch)
                    except Exception as exc:  # noqa: BLE001 - branch may be gone already
                        self.store.log("error", issue_number=number, detail=f"delete_branch: {exc}")
            if issue.get("state") == "open":
                self.github.close_issue(number)
            self.store.upsert_task(number, cleaned_up=1, settled_at=time.time())
            self.store.log("rejected", issue_number=number, detail=issue.get("title", ""))
            cleaned += 1
        return cleaned
