"""Watch Devin and GitHub, write down what happened, clean up rejected work.

Devin Automations start every session, so nothing here dispatches: one pass
reads the issues and sessions, decides what each one means, and records it.
The decisions are pure functions at the top of the file, so they are testable
without a network; :class:`Watcher` is the I/O around them.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, NamedTuple

from shared.github import pr_number_from_url

from .store import Store

# A session's lifecycle is two fields: `status` is coarse, and a session that
# has finished its task still reads `running` with `status_detail: finished`.
TERMINAL_STATUSES = {"exit", "error", "suspended"}
FINISHED_DETAIL = "finished"
BLOCKED_DETAILS = {"waiting_for_user", "waiting_for_approval"}
# Suspended for lack of budget rather than lack of work.
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


class PrFacts(NamedTuple):
    state: str | None
    opened_at: float | None
    merged_at: float | None


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


def epoch(value: Any) -> float | None:
    """Epoch seconds from the ISO strings both APIs send. None if unparseable —
    a zero would look like 1970 and age every session past its timeout."""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _started_at(session: dict[str, Any]) -> float | None:
    return epoch(session.get("created_at"))


def structured(session: dict[str, Any]) -> dict[str, Any]:
    """The session's own report of what it did.

    The platform leaves ``structured_output`` null on automation-spawned
    sessions, so :class:`Watcher` fills it in from the final message before
    these functions see it. One place to read either way.
    """
    out = session.get("structured_output")
    return out if isinstance(out, dict) else {}


def issue_number_for(session: dict[str, Any]) -> int | None:
    """Which issue a session is working on, or None while it has not said yet."""
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
    # The API's own view, for a session that opened a PR but never reported it.
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
        # Both halves: "running" alone hides working vs. waiting vs. done.
        "session_status": f"{status}/{detail}" if detail else status,
        "classification": out.get("classification"),
        "validate_command": out.get("validate_command"),
        "validate_registry": out.get("validate_command_from_registry"),
        "outcome": out.get("outcome"),
        "pr_url": pr_url_for(session),
        "acus": session.get("acus_consumed"),
    }
    if "validation_passed" in out:
        update["validation_passed"] = 1 if out.get("validation_passed") else 0
    return {k: v for k, v in update.items() if v is not None}


def stage_for_session(session: dict[str, Any], pr_state: str | None = None) -> str:
    """The furthest stage this session's evidence supports.

    Conservative on purpose: a finished session is not a success. Only a
    validated PR reaches ``verified``, and only GitHub reaches ``merged``.
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
    # Its own reason: the work never got a fair attempt at being fixed.
    if detail in BUDGET_DETAILS:
        return "budget_exhausted"
    if is_done(session) and not out:
        return "no_output"
    started = _started_at(session)
    if not is_done(session) and started is not None:
        if (now or time.time()) - started > timeout_seconds:
            return "timed_out"
    return None


class Watcher:
    def __init__(self, store: Store, devin: Any, github: Any, config: Any) -> None:
        self.store = store
        self.devin = devin
        self.github = github
        self.config = config
        # Scan sessions are attached to no issue by nature, so they are logged
        # once rather than on every one of the day's ~2,800 cycles.
        self._unattached: set[str] = set()

    # ------------------------------------------------------------------ pass

    def cycle(self) -> dict[str, int]:
        """One pass. Never raises: a failed cycle records itself and the
        dashboard's freshness stamp is what tells you."""
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
        self.store.set_meta("last_checked", str(time.time()))
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
                # When GitHub says it was filed, not when this database first
                # saw it — otherwise a day-long cycle reads as a few seconds on
                # a service started after the work.
                detected_at=epoch(issue.get("created_at")),
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
        # Oldest first, so when an issue has several attempts the row ends the
        # cycle describing the newest one.
        for session in sorted(sessions, key=lambda s: _started_at(s) or 0.0):
            session = {
                **session,
                "structured_output": self.devin.report(session),
                "acus_consumed": self.devin.session_acus(str(session.get("session_id") or "")),
            }
            session_id = session.get("session_id")
            number = issue_number_for(session)
            if number is None:
                if session_id not in self._unattached:
                    self._unattached.add(str(session_id))
                    self.store.log(
                        "session_unattached",
                        session_id=session_id,
                        detail="/".join(p for p in _status(session) if p),
                    )
                continue
            task = self.store.get_task(number)
            update = task_update_from_session(session)
            seen = self.store.session_ids_for(number)
            if session_id and session_id not in seen:
                # A second session on the same issue is rework. Counting
                # distinct session ids, rather than observations, is what keeps
                # attempts-per-issue from climbing on every poll.
                update["attempts"] = len(seen) + 1
                if seen:
                    self.store.log(
                        "attempt", issue_number=number, session_id=session_id, detail="rework"
                    )
                else:
                    self.store.log("dispatched", issue_number=number, session_id=session_id)
            self.store.upsert_task(number, **update)

            reason = failure_reason(session, self.config.session_timeout_seconds)
            if reason:
                self.store.upsert_task(number, failure_reason=reason)
            facts = self.pr_facts(
                update.get("pr_url") or (task or {}).get("pr_url"),
                (task or {}).get("pr_state"),
            )
            stage = stage_for_session(session, facts.state)
            if facts.state:
                self.store.upsert_task(number, pr_state=facts.state)
            # GitHub's timestamps, not this loop's clock, which only knows when
            # it looked. Never overwritten once known.
            if not (task or {}).get("pr_opened_at"):
                self.store.upsert_task(number, pr_opened_at=facts.opened_at)
            if not (task or {}).get("settled_at"):
                settled = facts.merged_at or (
                    facts.opened_at if stage == "verified" else None
                )
                self.store.upsert_task(number, settled_at=settled)
            self.store.advance(number, stage)
        return len(sessions)

    def pr_facts(self, pr_url: str | None, known: str | None = None) -> PrFacts:
        """What GitHub says about the PR: its state and its own timestamps.

        Merged is terminal, so a merged PR is remembered rather than re-fetched
        every cycle for the rest of its life.
        """
        if known == "merged" or not pr_url:
            return PrFacts(known if known == "merged" else None, None, None)
        number = pr_number_from_url(pr_url)
        if number is None:
            return PrFacts(None, None, None)
        try:
            pr = self.github.get_pull_request(number)
        except Exception as exc:  # noqa: BLE001
            self.store.log("error", detail=f"pr_state {pr_url}: {exc}")
            return PrFacts(None, None, None)
        merged_at = epoch(pr.get("merged_at"))
        return PrFacts(
            "merged" if merged_at else pr.get("state"),
            epoch(pr.get("created_at")),
            merged_at,
        )

    # --------------------------------------------------------------- cleanup

    def cleanup_rejected(self) -> int:
        """A human labelled the issue ``devin:rejected``: close the PR, delete
        the branch, close the issue.

        Recorded as *rejected*, not *failed*: failure means the fix was bad,
        rejection means the work should never have been filed.
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
