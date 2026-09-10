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

from shared.github import pr_number_from_url, repo_from_pr_url

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

# A scan reports no outcome, only what it did, so these stand in for one.
REPORTED_KEYS = frozenset({"issues_filed", "classes_proposed"})

# Reporting one of these is not the same as being finished with it.
OUTCOME_STATUS = {"blocked": "waiting on you", "abandoned": "gave up"}

# A remediation's own verdict, said plainly. The raw values are schema enums.
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

# The registry lives in the fork. A PR that touches nothing else is a class
# proposal — the scan's output when it has no rules yet — not a fix.
REGISTRY_DIR = ".devin/classifications/"

# How often a session that is still working says so. Long enough not to bury
# the state changes, short enough that a quiet terminal never means "stuck".
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


def session_url(session: dict[str, Any]) -> str:
    """Where to watch this session work."""
    url = session.get("url")
    if isinstance(url, str) and url:
        return url
    session_id = str(session.get("session_id") or "")
    return f"https://app.devin.ai/sessions/{session_id.removeprefix('devin-')}"


def role(session: dict[str, Any]) -> str:
    """scan or remediate. Tagged at creation, so it is known from the first
    cycle — before the session has said anything about what it is doing."""
    for tag in session.get("tags") or []:
        if str(tag).startswith("role:"):
            return str(tag)[len("role:") :]
    return ""


def is_scan(session: dict[str, Any]) -> bool:
    """Untagged, fall back to the older test: a scan works on the repo, so it
    is the session that never names an issue."""
    tagged = role(session)
    return tagged == "scan" if tagged else issue_number_for(session) is None


def human_status(session: dict[str, Any]) -> str:
    """The status in the words someone watching would use.

    The API's own pair is misleading read literally: a session that has done
    its job and gone quiet reports `running/waiting_for_user`, and one Devin
    idled out reports `suspended/inactivity`. What a person wants to know is
    whether to wait, to answer, or to go and look.
    """
    status, detail = _status(session)
    if status == "error":
        return "failed"
    if detail in BUDGET_DETAILS:
        return "out of budget"
    # A session that has filed its report is done, whatever it says
    # afterwards — and what it says afterwards is `waiting_for_user`.
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
    """A session as a person reading the dashboard needs it: what kind of work,
    what it is working on, how it is going, and what has come of it.

    Devin names the session after the task it was given, which is a better
    answer to "what is it doing" than anything this side could reconstruct.
    """
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
        # Time since Devin last touched it. A working session that has been
        # silent far longer than it has been alive is the one worth opening.
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
    """What a finished scan produced, in one line.

    A scan is the only session whose result is not a PR on an issue, so
    without this its whole output is a session someone has to open and read.
    """
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

    The project tag is the same on every fork, so without this a run against a
    new fork inherits the sessions of the one before it. Sessions that predate
    the tag are judged by the PR they opened instead, and only one that names
    no repo at all is left visible rather than silently dropped.
    """
    tagged = [t for t in session.get("tags") or [] if str(t).startswith("repo:")]
    if tagged:
        return f"repo:{repo}" in tagged
    url = pr_url_for(session)
    named = repo_from_pr_url(url) if url else None
    return named in (None, repo)


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


def waiting_item(
    pr: dict[str, Any],
    task: dict[str, Any] | None,
    paths: list[str] | None,
    now: float | None = None,
) -> dict[str, Any]:
    """An open PR as a to-do: what it is, and what the human does with it.

    The loop stops at every one of these by design, so a PR nobody has looked
    at is not a stalled system — but it is indistinguishable from one unless
    the dashboard says whose turn it is.
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
                "you asked for changes — Devin answers on the PR"
                if (task.get("changes_requested") or 0)
                else "merge it — validation passed"
                if passed == 1
                else "rework or reject — validation did not pass"
                if passed == 0
                else "review — the session never reported a validation run"
            ),
        }
    if paths and all(p.startswith(REGISTRY_DIR) for p in paths):
        return {
            **item,
            "kind": "class proposal",
            "what": f"{len(paths)} detection class(es)",
            "do": "merge to switch detection on, or comment to revise",
        }
    return {**item, "kind": "pull request", "what": "", "do": "review"}


def opened_by_devin(pr: dict[str, Any]) -> bool:
    """The app's login, not a name match: a contributor called devin-something
    opened their PR for their own reasons."""
    return str((pr.get("user") or {}).get("login", "")).startswith("devin-ai-integration")


class Watcher:
    def __init__(self, store: Store, devin: Any, github: Any, config: Any) -> None:
        self.store = store
        self.devin = devin
        self.github = github
        self.config = config
        # Scan sessions are attached to no issue by nature. Logged on the two
        # cycles that mean something rather than all ~2,800 of the day's.
        self._scans: dict[str, str] = {}
        # When each in-flight session last said it was still working, and which
        # ones have already reported being stuck.
        self._beats: dict[str, float] = {}
        self._stuck: set[str] = set()

    # ------------------------------------------------------------------ pass

    def cycle(self) -> dict[str, int]:
        """One pass. Never raises: a failed cycle records itself and the
        dashboard's freshness stamp is what tells you."""
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
        sessions = [
            s
            for s in self.devin.list_sessions(tags=[self.config.session_tag])
            if belongs_to(s, self.config.repo)
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
            # Every session gets a row of its own. A remediation only names its
            # issue once it reports, and until then the task table cannot show
            # it at all — which is exactly the hour someone is watching.
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
                # A second session on the same issue is rework. Counting
                # distinct session ids, rather than observations, is what keeps
                # attempts-per-issue from climbing on every poll.
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
            self.note_progress(number, session, stage)
        self.store.set_sessions(
            sorted(views, key=lambda s: s["started_at"] or 0.0, reverse=True)[:8]
        )
        return len(sessions)

    def sync_waiting(self) -> int:
        """The human's queue: open PRs the loop will not advance on its own.

        Read from GitHub rather than inferred from the funnel, because a PR
        merged or closed in the browser has to leave this list, and because a
        class proposal belongs to a scan that will scroll off the session view
        long before anyone gets to it.
        """
        by_url = {t["pr_url"]: t for t in self.store.tasks() if t.get("pr_url")}
        items = []
        for pr in self.github.open_pull_requests():
            task = by_url.get(pr.get("html_url"))
            if task is None and not opened_by_devin(pr):
                continue  # someone else's PR is not this loop's business
            if task:
                task = {**task, "changes_requested": self.note_review(pr["number"], task)}
            paths = None if task else self.github.pull_request_paths(pr["number"])
            items.append(waiting_item(pr, task, paths))
        self.store.set_waiting(items)
        return len(items)

    def note_review(self, number: int, task: dict[str, Any]) -> int:
        """Count of times a human sent this PR back, recorded and not acted on.

        Nothing here reruns the work: Devin's own comment monitoring answers
        reviews on the PR its session opened, and a second trigger would only
        race it. What the loop owes you is the number — how often a first
        attempt was not good enough is the measure of whether it is working.
        """
        count = self.github.changes_requested(number)
        before = task.get("changes_requested") or 0
        if count > before:
            self.store.upsert_task(task["issue_number"], changes_requested=count)
            self.store.log(
                "changes_requested",
                issue_number=task["issue_number"],
                detail=f"sent back by a human ({count}) — {task.get('pr_url') or ''}",
            )
        return count

    def log_scan(self, session: dict[str, Any]) -> None:
        """A scan's two moments: it started, and here is what came of it."""
        session_id = str(session.get("session_id") or "")
        # By its own status a scan never ends: it reports, then sits in
        # `waiting_for_user` forever. Having reported is what finished means.
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
        """Say that a session is still working, or that it is waiting on a human.

        A remediation can run for the better part of an hour, and between
        `dispatched` and its PR it produces no state change at all. Without
        this, a working session and a dead one look the same from the terminal.
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
        # The first sighting needs no heartbeat: `dispatched` just said all of
        # this, on the same line.
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
        """What GitHub says about the PR: its state and its own timestamps.

        Merged is terminal, so a merged PR is remembered rather than re-fetched
        every cycle for the rest of its life.
        """
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
