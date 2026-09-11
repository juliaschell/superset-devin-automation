"""Watch Devin and GitHub, record what happened, clean up rejected work.

Devin Automations start every session, so nothing here dispatches. The
decisions are pure functions at the top of the file and testable without a
network; :class:`Watcher` is the I/O around them.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, NamedTuple

from shared.github import pr_number_from_url, repo_from_pr_url

from .store import Store

# A session that has finished still reads status `running`, detail `finished`.
TERMINAL_STATUSES = {"exit", "error", "suspended"}
FINISHED_DETAIL = "finished"
BLOCKED_DETAILS = {"waiting_for_user", "waiting_for_approval"}
# Suspended for lack of budget, not lack of work.
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

# A scan reports counts rather than an outcome; these stand in for one.
REPORTED_KEYS = frozenset({"issues_filed", "classes_proposed"})

# Reporting one of these is not the same as being finished with it.
OUTCOME_STATUS = {"blocked": "waiting on you", "abandoned": "gave up"}

# The schema's outcome enums, in plain words.
OUTCOME_WORDS = {
    "pr_opened_validated": "PR opened, validation passed",
    "pr_opened_validation_failed": "PR opened, validation failed",
    "no_matching_class": "nothing matched an adopted class",
    "blocked": "needs a human",
    "abandoned": "gave up",
}

OUTCOME_TO_STAGE = {
    "pr_opened_validated": "verified",
    "pr_opened_validation_failed": "pr_open",
}

# A PR touching only this directory is a class proposal, not a fix.
REGISTRY_DIR = ".devin/classifications/"

# How often a still-working session says so, so silence never means "stuck".
HEARTBEAT_SECONDS = 300

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
    """Epoch seconds from an ISO string. None if unparseable: a 0 would read as
    1970 and time every session out."""
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
    """The session's own report. ``structured_output`` is null on
    automation-spawned sessions, so :class:`Watcher` fills it in from the final
    message before these functions see it."""
    out = session.get("structured_output")
    return out if isinstance(out, dict) else {}


def session_url(session: dict[str, Any]) -> str:
    url = session.get("url")
    if isinstance(url, str) and url:
        return url
    session_id = str(session.get("session_id") or "")
    return f"https://app.devin.ai/sessions/{session_id.removeprefix('devin-')}"


def role(session: dict[str, Any]) -> str:
    """Return "scan" or "remediate", from the tag set at session creation."""
    for tag in session.get("tags") or []:
        if str(tag).startswith("role:"):
            return str(tag)[len("role:") :]
    return ""


def is_scan(session: dict[str, Any]) -> bool:
    """Untagged sessions fall back to: a scan is the one naming no issue."""
    tagged = role(session)
    return tagged == "scan" if tagged else issue_number_for(session) is None


def human_status(session: dict[str, Any]) -> str:
    """Whether to wait, to answer, or to go and look.

    Read literally the API's own pair misleads: a session that finished and
    went quiet reports `running/waiting_for_user`, and one Devin idled out
    reports `suspended/inactivity`.
    """
    status, detail = _status(session)
    if status == "error":
        return "failed"
    if detail in BUDGET_DETAILS:
        return "out of budget"
    # Having reported is what finished means, whatever the status says after.
    out = structured(session)
    outcome = str(out.get("outcome") or "")
    # Two of the outcomes a remediation can report are not endings.
    if outcome in OUTCOME_STATUS:
        return OUTCOME_STATUS[outcome]
    if outcome or out.keys() & REPORTED_KEYS or detail == FINISHED_DETAIL or status == "exit":
        return "finished"
    if is_blocked(session):
        return "waiting on you"
    if status == "suspended":
        return "stopped early"
    return "working"


def duration(seconds: float | None) -> str:
    """Elapsed time as a person would say it."""
    if seconds is None or seconds < 0:
        return ""
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours, minutes = divmod(int(seconds // 60), 60)
    return f"{hours}h {minutes}m"


def session_result(session: dict[str, Any]) -> str:
    """What the session has to show for itself so far."""
    out = structured(session)
    if is_scan(session):
        return scan_counts(out)
    outcome = out.get("outcome")
    return OUTCOME_WORDS.get(str(outcome), str(outcome or ""))


def session_view(session: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """One row of the dashboard's session table: what kind of work, how it is
    going, and what has come of it."""
    now = now or time.time()
    started = _started_at(session)
    updated = epoch(session.get("updated_at"))
    return {
        "session_id": str(session.get("session_id") or ""),
        "kind": "scan" if is_scan(session) else "fix",
        "issue_number": issue_number_for(session),
        "title": session.get("title") or "(unnamed session)",
        "url": session_url(session),
        "status": human_status(session),
        "started_at": started,
        "running_for": duration(now - started) if started else "",
        # Silent far longer than it has been alive is the one worth opening.
        "quiet_for": duration(now - updated) if updated else "",
        "pr_url": pr_url_for(session),
        "result": session_result(session),
        "acus": session.get("acus_consumed") or None,
    }


def scan_counts(out: dict[str, Any]) -> str:
    """How much a scan got through, as counts."""
    counts = {
        "issues filed": out.get("issues_filed"),
        "classes proposed": out.get("classes_proposed"),
        "skipped": out.get("skipped"),
    }
    return ", ".join(f"{len(v)} {name}" for name, v in counts.items() if isinstance(v, list) and v)


def scan_summary(session: dict[str, Any]) -> str:
    """What a finished scan produced, in one line. A scan is the only session
    whose result is not a PR, so otherwise it has to be opened and read."""
    out = structured(session)
    if not out:
        return "no report"
    line = scan_counts(out)
    urls = sorted(
        {
            item["pr_url"]
            for item in out.get("classes_proposed") or []
            if isinstance(item, dict) and item.get("pr_url")
        }
    )
    return ", ".join(filter(None, [line or "nothing to do", *urls]))


def belongs_to(session: dict[str, Any], repo: str) -> bool:
    """Whether this session is work on ``repo``.

    The project tag is the same on every fork, so without this a new fork
    inherits the last one's sessions. Sessions older than the tag are judged by
    the PR they opened; one naming no repo at all stays visible.
    """
    tagged = [t for t in session.get("tags") or [] if str(t).startswith("repo:")]
    if tagged:
        return f"repo:{repo}" in tagged
    url = pr_url_for(session)
    named = repo_from_pr_url(url) if url else None
    return named in (None, repo)


def predates(session: dict[str, Any], born: float | None) -> bool:
    """Whether this session ran before the current fork existed.

    A `repo:` tag names a fork but not which one: a fork deleted and remade
    under the same name reads identically, and its old sessions come back
    reporting issues and PRs that no longer exist.
    """
    started = _started_at(session)
    return bool(born and started and started < born)


def fork_identity(github: Any) -> tuple[str | None, float | None]:
    """GitHub's id for the fork, and when it was created.

    Nothing if GitHub cannot be reached: an unreachable API is not evidence the
    fork changed, and emptying the database on it would lose a run.
    """
    try:
        repo = github.repository()
    except Exception:  # noqa: BLE001 - any failure to read it means "unknown"
        return None, None
    return str(repo.get("id") or "") or None, epoch(repo.get("created_at"))


def issue_number_for(session: dict[str, Any]) -> int | None:
    """Which issue a session is working on, or None until it says."""
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
    # For a session that opened a PR but never reported it.
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
        # Both halves: "running" alone hides working vs waiting vs done.
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
    """The furthest stage this session's evidence supports. A finished session
    is not a success: only a validated PR reaches ``verified``, and only
    GitHub reaches ``merged``."""
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
    # Its own reason: the work never got a fair attempt.
    if detail in BUDGET_DETAILS:
        return "budget_exhausted"
    if is_done(session) and not out:
        return "no_output"
    started = _started_at(session)
    if not is_done(session) and started is not None:
        if (now or time.time()) - started > timeout_seconds:
            return "timed_out"
    return None


def waiting_item(
    pr: dict[str, Any],
    task: dict[str, Any] | None,
    paths: list[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """An open PR as a to-do: what it is, and what the human does with it.

    The loop stops at every one of these by design, which is indistinguishable
    from a stalled system unless the dashboard says whose turn it is.
    """
    opened = epoch(pr.get("created_at"))
    item = {
        "url": pr.get("html_url") or "",
        "title": pr.get("title") or "",
        "waiting_for": duration((now or time.time()) - opened) if opened else "",
    }
    if task:
        passed = task.get("validation_passed")
        return {
            **item,
            "kind": f"fix for #{task['issue_number']}",
            "what": task.get("classification") or task.get("title") or "",
            "do": (
                "sent back — Devin is answering"
                if task.get("awaiting_devin")
                else "re-review — Devin answered"
                if (task.get("changes_requested") or 0)
                else "merge"
                if passed == 1
                else "rework or reject"
                if passed == 0
                else "review — validation not reported"
            ),
        }
    if paths and all(p.startswith(REGISTRY_DIR) for p in paths):
        return {
            **item,
            "kind": "class proposal",
            "what": f"{len(paths)} detection class(es)",
            "do": "merge or comment",
        }
    return {**item, "kind": "pull request", "what": "", "do": "review"}


def opened_by_devin(pr: dict[str, Any]) -> bool:
    """Match the app's login, not the name: a human contributor called
    devin-something opened their PR for their own reasons."""
    return str((pr.get("user") or {}).get("login", "")).startswith("devin-ai-integration")


class Watcher:
    def __init__(self, store: Store, devin: Any, github: Any, config: Any) -> None:
        self.store = store
        self.devin = devin
        self.github = github
        self.config = config
        # A scan belongs to no issue, so it is logged on the two cycles that
        # mean something rather than on all ~2,800 of the day's.
        self._scans: dict[str, str] = {}
        # When each in-flight session last said it was still working, and which
        # ones have already reported being stuck.
        self._beats: dict[str, float] = {}
        self._stuck: set[str] = set()

    # ------------------------------------------------------------------ pass

    def cycle(self) -> dict[str, int]:
        """One pass. Never raises: a failed cycle records itself, and the
        dashboard's freshness stamp is what shows it."""
        stats = {"issues": 0, "sessions": 0, "waiting": 0, "cleaned": 0, "errors": 0}
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
            stats["waiting"] = self.sync_waiting()
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            self.store.log("error", detail=f"sync_waiting: {exc}")
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
                # GitHub's timestamp, not ours: otherwise a day-long cycle
                # reads as seconds on a service started after the work.
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
        born = self.store.fork_born()
        sessions = [
            s
            for s in self.devin.list_sessions(tags=[self.config.session_tag])
            if belongs_to(s, self.config.repo) and not predates(s, born)
        ]
        views: list[dict[str, Any]] = []
        # Oldest first, so when an issue has several attempts the row ends the
        # cycle describing the newest one.
        for session in sorted(sessions, key=lambda s: _started_at(s) or 0.0):
            session = {
                **session,
                "structured_output": self.devin.report(session),
                "acus_consumed": self.devin.session_acus(str(session.get("session_id") or "")),
            }
            session_id = session.get("session_id")
            # Every session gets a row. A remediation does not name its issue
            # until it reports, so until then the task table cannot show it —
            # which is exactly the hour someone is watching.
            views.append(session_view(session))
            number = issue_number_for(session)
            if number is None:
                if is_scan(session):
                    self.log_scan(session)
                continue
            task = self.store.get_task(number)
            update = task_update_from_session(session)
            seen = self.store.session_ids_for(number)
            if session_id and session_id not in seen:
                # A second session on the same issue is rework. Counted by
                # distinct id so it does not climb on every poll.
                update["attempts"] = len(seen) + 1
                if seen:
                    self.store.log(
                        "attempt", issue_number=number, session_id=session_id, detail="rework"
                    )
                else:
                    self.store.log(
                        "dispatched",
                        issue_number=number,
                        session_id=session_id,
                        detail=session_url(session),
                    )
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
            # GitHub's timestamps, not this loop's clock. Never overwritten.
            if not (task or {}).get("pr_opened_at"):
                self.store.upsert_task(number, pr_opened_at=facts.opened_at)
            if not (task or {}).get("settled_at"):
                settled = facts.merged_at or (
                    facts.opened_at if stage == "verified" else None
                )
                self.store.upsert_task(number, settled_at=settled)
            self.store.advance(number, stage)
            self.note_progress(number, session, stage)
        self.store.set_sessions(
            sorted(views, key=lambda s: s["started_at"] or 0.0, reverse=True)[:8]
        )
        return len(sessions)

    def sync_waiting(self) -> int:
        """The human's queue: open PRs the loop will not advance on its own.

        Read from GitHub rather than inferred from the funnel, so that a PR
        merged in the browser leaves the list, and so that a class proposal
        outlives the scan session that opened it.
        """
        by_url = {t["pr_url"]: t for t in self.store.tasks() if t.get("pr_url")}
        items = []
        for pr in self.github.open_pull_requests():
            task = by_url.get(pr.get("html_url"))
            if task is None and not opened_by_devin(pr):
                continue  # someone else's PR is not this loop's business
            if task:
                count, answering = self.note_review(pr["number"], task)
                task = {**task, "changes_requested": count, "awaiting_devin": answering}
            paths = None if task else self.github.pull_request_paths(pr["number"])
            items.append(waiting_item(pr, task, paths))
        self.store.set_waiting(items)
        return len(items)

    def note_review(self, number: int, task: dict[str, Any]) -> tuple[int, bool]:
        """Times a human sent this PR back, and whether Devin has answered the
        last one. Counted, never acted on.

        Devin's own comment monitoring answers reviews on the PR its session
        opened, so a trigger here would only race it. The number is what the
        loop owes you: how often a first attempt was not good enough.
        """
        count, reviewed_at = self.github.changes_requested(number)
        before = task.get("changes_requested") or 0
        if count > before:
            self.store.upsert_task(task["issue_number"], changes_requested=count)
            self.store.log(
                "changes_requested",
                issue_number=task["issue_number"],
                detail=f"sent back by a human ({count}) — {task.get('pr_url') or ''}",
            )
        if not count:
            return 0, False
        # A commit or a reply hands the PR back: an answer explaining why no
        # change is needed is still an answer.
        reviewed = epoch(reviewed_at)
        answered = epoch(self.github.last_devin_activity_at(number))
        return count, reviewed is not None and (answered is None or answered < reviewed)

    def log_scan(self, session: dict[str, Any]) -> None:
        """A scan's two moments: it started, and here is what came of it."""
        session_id = str(session.get("session_id") or "")
        # By its own status a scan never ends: it reports, then sits in
        # `waiting_for_user`. Having reported is what finished means.
        phase = "finished" if human_status(session) == "finished" else "started"
        seen = self._scans.get(session_id)
        if phase == seen or seen == "finished":
            return
        self._scans[session_id] = phase
        self.store.log(
            f"scan_{phase}",
            session_id=session_id,
            detail=scan_summary(session) if phase == "finished" else session_url(session),
        )

    def note_progress(self, number: int, session: dict[str, Any], stage: str) -> None:
        """Say that a session is still working, or that it needs a human.

        A remediation can run the better part of an hour with no state change
        between `dispatched` and its PR, and from the terminal that looks the
        same as a dead one.
        """
        session_id = str(session.get("session_id") or "")
        if is_blocked(session):
            if session_id not in self._stuck:
                self._stuck.add(session_id)
                self.store.log(
                    "waiting",
                    issue_number=number,
                    session_id=session_id,
                    detail=f"needs an answer from you — {session_url(session)}",
                )
            return
        if is_done(session) or stage != "running":
            self._beats.pop(session_id, None)
            return
        now = time.time()
        # The first sighting needs no heartbeat: `dispatched` just said it.
        since = self._beats.setdefault(session_id, now)
        if now - since < HEARTBEAT_SECONDS:
            return
        self._beats[session_id] = now
        started = _started_at(session)
        minutes = f"{int((now - started) // 60)}m " if started else ""
        self.store.log(
            "working",
            issue_number=number,
            session_id=session_id,
            detail=f"{minutes}{session_url(session)}",
        )

    def pr_facts(self, pr_url: str | None, known: str | None = None) -> PrFacts:
        """The PR's state and GitHub's own timestamps for it. Merged is
        terminal, so a merged PR is remembered rather than re-fetched."""
        if known == "merged" or not pr_url:
            return PrFacts(known if known == "merged" else None, None, None)
        if repo_from_pr_url(pr_url) not in (None, self.config.repo):
            return PrFacts(None, None, None)
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

        Recorded as rejected, not failed: failure means the fix was bad,
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
            if pr_url and repo_from_pr_url(pr_url) not in (None, self.config.repo):
                pr_url = None
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
