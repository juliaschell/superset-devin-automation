from __future__ import annotations

import time
from typing import Any

from tracker.store import Store
from tracker.watch import (
    HEARTBEAT_SECONDS,
    Watcher,
    belongs_to,
    duration,
    failure_reason,
    human_status,
    issue_number_for,
    scan_summary,
    session_view,
    stage_for_session,
    task_update_from_session,
)


class FakeConfig:
    repo = "o/r"
    ready_label = "devin:ready"
    rejected_label = "devin:rejected"
    session_tag = "superset-remediation"
    session_timeout_seconds = 3600


class FakeDevin:
    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        self._sessions = sessions

    def list_sessions(self, tags: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._sessions

    def report(self, session: dict[str, Any]) -> dict[str, Any]:
        out = session.get("structured_output")
        return out if isinstance(out, dict) else {}

    def session_acus(self, session_id: str) -> float | None:
        for session in self._sessions:
            if session.get("session_id") == session_id:
                acus = session.get("acus_consumed")
                return float(acus) if isinstance(acus, int | float) else None
        return None


class FakeGitHub:
    def __init__(self, issues: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.issues = issues or {}
        self.pull_requests: dict[int, dict[str, Any]] = {}
        self.calls: list[str] = []

    def issues_with_label(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        return self.issues.get(label, [])

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return self.pull_requests.get(number, {"state": "open", "head": {"ref": "devin/fix"}})

    def close_pull_request(self, number: int) -> None:
        self.calls.append(f"close_pr:{number}")

    def close_issue(self, number: int) -> None:
        self.calls.append(f"close_issue:{number}")

    def delete_branch(self, branch: str) -> None:
        self.calls.append(f"delete_branch:{branch}")


def issue(number: int, label: str = "devin:ready") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"issue {number}",
        "html_url": f"https://github.com/o/r/issues/{number}",
        "state": "open",
        "created_at": "2026-09-01T00:00:00Z",
        "labels": [{"name": label}],
    }


def session(number: int, outcome: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"issue_number": number}
    if outcome:
        out["outcome"] = outcome
    out.update(extra.pop("output", {}))
    return {
        "session_id": extra.pop("session_id", f"session-{number}"),
        "status": extra.pop("status", "running"),
        "status_detail": extra.pop("status_detail", "finished"),
        "tags": ["superset-remediation"],
        "structured_output": out,
        **extra,
    }


# ------------------------------------------------------------- pure functions


def test_issue_number_from_structured_output_then_tag():
    assert issue_number_for(session(7)) == 7
    assert issue_number_for({"tags": ["issue-9"], "structured_output": {}}) == 9
    assert issue_number_for({"tags": [], "structured_output": {}}) is None


def test_finished_session_without_a_pr_is_not_success():
    """The most important negative case: 'the session ended' is not an outcome."""
    assert stage_for_session(session(1)) == "dispatched"


def test_completion_is_read_from_status_detail_not_status():
    """A session that has finished its task still reports status 'running'.
    Reading status alone leaves every completed session counted as in flight."""
    working = session(1, status="running", status_detail="working")
    assert stage_for_session(working) == "running"
    assert stage_for_session(session(1, status="running", status_detail="finished")) == "dispatched"
    assert stage_for_session(session(1, status="exit", status_detail="")) == "dispatched"


def test_validated_pr_reaches_verified_but_not_merged():
    s = session(1, "pr_opened_validated", output={"pr_url": "https://github.com/o/r/pull/3"})
    assert stage_for_session(s) == "verified"
    assert stage_for_session(s, pr_state="merged") == "merged"


def test_failed_validation_stops_at_pr_open():
    s = session(1, "pr_opened_validation_failed", output={"pr_url": "https://github.com/o/r/pull/3"})
    assert stage_for_session(s) == "pr_open"
    assert failure_reason(s, 3600) == "verification_failed"


def test_failure_taxonomy_distinguishes_causes():
    assert failure_reason(session(1, "no_matching_class"), 3600) == "no_matching_class"
    waiting = {"status": "running", "status_detail": "waiting_for_user", "structured_output": {}}
    assert failure_reason(waiting, 3600) == "blocked_on_human"
    assert failure_reason(session(1, status="error", status_detail="", output={}), 3600) == "session_error"
    # Out of budget is an operational failure, not "Devin could not fix it".
    broke = {"status": "suspended", "status_detail": "out_of_credits", "structured_output": {}}
    assert failure_reason(broke, 3600) == "budget_exhausted"
    assert failure_reason({"status": "exit", "structured_output": {}}, 3600) == "no_output"
    assert failure_reason(session(1, "pr_opened_validated"), 3600) is None


def test_timeout_is_detected_from_age():
    stuck = {"status": "running", "status_detail": "working", "structured_output": {}, "created_at": 0}
    assert failure_reason(stuck, timeout_seconds=10, now=1000) == "timed_out"


def test_task_update_carries_both_validation_commands():
    s = session(
        1,
        "pr_opened_validated",
        output={
            "validate_command": "pytest -q tests/x",
            "validate_command_from_registry": "pytest -q tests/y",
        },
    )
    update = task_update_from_session(s)
    assert update["validate_command"] != update["validate_registry"]


# ----------------------------------------------------------------- the cycle


def build(tmp_path, sessions=None, issues=None):
    store = Store(str(tmp_path / "s.db"))
    github = FakeGitHub(issues or {})
    return store, github, Watcher(store, FakeDevin(sessions or []), github, FakeConfig())


def test_detection_then_dispatch_is_recorded_once(tmp_path):
    store, _, watcher = build(
        tmp_path,
        sessions=[session(1, "pr_opened_validated", output={"pr_url": "https://github.com/o/r/pull/5"})],
        issues={"devin:ready": [issue(1)]},
    )
    watcher.cycle()
    watcher.cycle()
    assert len([e for e in store.events() if e["kind"] == "detected"]) == 1
    assert store.get_task(1)["attempts"] == 1
    assert store.get_task(1)["stage"] == "verified"


def test_second_session_on_one_issue_counts_as_rework(tmp_path):
    """A human requesting changes fires a new session; that is attempt 2 of the
    same issue, not a duplicate task."""
    store, _, watcher = build(tmp_path, issues={"devin:ready": [issue(1)]})
    watcher.devin = FakeDevin([session(1, session_id="a")])
    watcher.cycle()
    watcher.devin = FakeDevin([session(1, session_id="b")])
    watcher.cycle()
    assert store.get_task(1)["attempts"] == 2
    assert store.get_task(1)["session_id"] == "b"


def test_two_sessions_on_one_issue_do_not_inflate_attempts_per_poll(tmp_path):
    """The API returns concurrent sessions in no particular order, so counting
    "this session differs from the one on the row" charges a fresh attempt every
    cycle. Attempts are distinct sessions, however often they are seen."""
    store, _, watcher = build(tmp_path, issues={"devin:ready": [issue(1)]})
    watcher.devin = FakeDevin([session(1, session_id="a"), session(1, session_id="b")])
    for _ in range(5):
        watcher.cycle()
    assert store.get_task(1)["attempts"] == 2


def test_a_scan_is_logged_when_it_starts_and_when_it_ends(tmp_path):
    """The two lines that let someone watching `make up` follow a scan without
    opening the session: it began, and this is what it produced."""
    store, _, watcher = build(tmp_path)
    running = session(None, session_id="scan", status_detail="working")
    watcher.devin = FakeDevin([running])
    for _ in range(3):
        watcher.cycle()

    done = session(
        None,
        session_id="scan",
        status_detail="finished",
        output={"classes_proposed": [{"slug": "a", "pr_url": "https://github.com/o/r/pull/2"}]},
    )
    watcher.devin = FakeDevin([done])
    for _ in range(3):
        watcher.cycle()

    kinds = [e["kind"] for e in store.events() if e["kind"].startswith("scan_")]
    assert kinds == ["scan_finished", "scan_started"]  # newest first
    finished = next(e for e in store.events() if e["kind"] == "scan_finished")
    assert finished["detail"] == "1 classes proposed, https://github.com/o/r/pull/2"


def test_a_remediation_says_where_to_watch_it_and_that_it_is_still_going(tmp_path):
    """A fix can run for an hour with no state change of its own. Without the
    link on dispatch and a heartbeat after it, working and dead look alike."""
    store, _, watcher = build(tmp_path, issues={"devin:ready": [issue(1)]})
    watcher.devin = FakeDevin([session(1, session_id="devin-abc", status_detail="working")])
    watcher.cycle()
    dispatched = next(e for e in store.events() if e["kind"] == "dispatched")
    assert dispatched["detail"] == "https://app.devin.ai/sessions/abc"

    watcher.cycle()
    assert not [e for e in store.events() if e["kind"] == "working"]  # too soon to repeat itself
    watcher._beats["devin-abc"] -= HEARTBEAT_SECONDS
    watcher.cycle()
    working = next(e for e in store.events() if e["kind"] == "working")
    assert working["detail"].endswith("https://app.devin.ai/sessions/abc")


def test_a_session_waiting_on_a_human_says_so_once(tmp_path):
    """The one silence that is not progress: nothing moves until someone
    answers, and no heartbeat would ever say that."""
    store, _, watcher = build(tmp_path, issues={"devin:ready": [issue(1)]})
    watcher.devin = FakeDevin([session(1, session_id="devin-abc", status_detail="waiting_for_user")])
    for _ in range(3):
        watcher.cycle()

    waiting = [e for e in store.events() if e["kind"] == "waiting"]
    assert len(waiting) == 1
    assert waiting[0]["detail"] == "needs an answer from you — https://app.devin.ai/sessions/abc"


def test_sessions_tagged_with_another_fork_are_not_this_run(tmp_path):
    """Every fork's sessions carry the same project tag, so a run against a new
    fork would otherwise report the previous fork's work as its own."""
    assert belongs_to({"tags": ["superset-remediation", "repo:o/r"]}, "o/r")
    assert not belongs_to({"tags": ["repo:o/old"]}, "o/r")
    assert belongs_to({"tags": ["superset-remediation"]}, "o/r")  # predates the tag

    store, _, watcher = build(tmp_path, issues={"devin:ready": [issue(1)]})
    watcher.devin = FakeDevin(
        [
            session(1, session_id="mine", tags=["repo:o/r"]),
            session(2, session_id="theirs", tags=["repo:o/old"]),
        ]
    )
    watcher.cycle()
    assert store.get_task(2) is None
    assert store.get_task(1)["session_id"] == "mine"


def test_scan_summary_says_so_when_a_scan_found_nothing():
    """An empty report is a result, and reads as a broken run if left blank."""
    assert scan_summary(session(None, output={"issues_filed": [], "classes_proposed": []})) == (
        "nothing to do"
    )
    assert scan_summary({"structured_output": {}}) == "no report"


def test_a_scan_is_visible_even_though_it_has_no_issue(tmp_path):
    """Until a scan files something there is no task, and a dashboard showing
    nothing is indistinguishable from one that is broken."""
    store, _, watcher = build(tmp_path)
    watcher.devin = FakeDevin([session(None, session_id="devin-abc")])
    watcher.cycle()
    (scan,) = store.sessions()
    assert scan["session_id"] == "devin-abc"
    assert scan["url"] == "https://app.devin.ai/sessions/abc"


def test_a_working_fix_is_on_the_dashboard_before_it_names_its_issue(tmp_path):
    """A remediation reports its issue number only at the end, so for the hour
    that someone actually wants to watch it, it has no task row."""
    store, _, watcher = build(tmp_path)
    watcher.devin = FakeDevin(
        [
            {
                "session_id": "devin-abc",
                "title": "Fix o/r#8 any types",
                "tags": ["superset-remediation", "role:remediate"],
                "status": "running",
                "status_detail": "working",
                "created_at": time.time() - 700,
                "structured_output": {},
            }
        ]
    )
    watcher.cycle()
    (view,) = store.sessions()
    assert view["kind"] == "fix"
    assert view["title"] == "Fix o/r#8 any types"
    assert view["status"] == "working"
    assert view["running_for"] == "11m"


def test_a_scan_that_has_reported_reads_as_finished_not_as_waiting():
    """Devin leaves a session that has said its piece in `waiting_for_user`,
    which read literally tells the human to go and answer a finished scan."""
    reported = session(
        None,
        status="running",
        status_detail="waiting_for_user",
        tags=["superset-remediation", "role:scan"],
        output={"issues_filed": [1, 2], "classes_proposed": []},
    )
    assert human_status(reported) == "finished"
    assert session_view(reported)["result"] == "2 issues filed"
    assert human_status(session(1, status="running", status_detail="waiting_for_user")) == (
        "waiting on you"
    )


def test_a_session_is_described_in_words_a_person_would_use():
    assert human_status(session(1, status="error", status_detail=None)) == "failed"
    assert human_status(session(1, status_detail="user_usage_limit_exceeded")) == "out of budget"
    assert human_status(session(1, status="suspended", status_detail="inactivity")) == (
        "stopped early"
    )
    assert human_status(session(1, "pr_opened_validated")) == "finished"
    # Reporting that it is stuck is not the same as being done.
    assert human_status(session(1, "blocked")) == "waiting on you"
    assert duration(45) == "45s" and duration(700) == "11m" and duration(7800) == "2h 10m"


def test_a_session_view_survives_a_session_that_says_almost_nothing():
    """Everything below is optional in the API, and a dashboard that raises is
    worse than one that says nothing."""
    view = session_view({"session_id": "devin-abc"})
    assert view["title"] == "(unnamed session)"
    assert view["running_for"] == "" and view["quiet_for"] == ""
    assert view["result"] == "" and view["acus"] is None
    assert view["url"] == "https://app.devin.ai/sessions/abc"


def test_rejected_issue_is_cleaned_up_and_distinguished_from_failure(tmp_path):
    store, github, watcher = build(
        tmp_path,
        sessions=[session(1, "pr_opened_validated", output={"pr_url": "https://github.com/o/r/pull/5"})],
        issues={"devin:ready": [issue(1)], "devin:rejected": [issue(1, "devin:rejected")]},
    )
    watcher.cycle()
    task = store.get_task(1)
    assert task["rejected"] == 1
    assert task["outcome"] == "human_rejected"
    assert task["failure_reason"] is None  # rejection is not a remediation failure
    assert github.calls == ["close_pr:5", "delete_branch:devin/fix", "close_issue:1"]


def test_cleanup_is_idempotent(tmp_path):
    store, github, watcher = build(
        tmp_path, issues={"devin:rejected": [issue(1, "devin:rejected")]}
    )
    watcher.cycle()
    watcher.cycle()
    assert github.calls.count("close_issue:1") == 1


def test_a_broken_api_does_not_kill_the_loop(tmp_path):
    """A cycle that throws must still stamp freshness data, so the dashboard can
    say it is stale instead of quietly serving old numbers."""
    store, _, watcher = build(tmp_path)

    class Boom:
        def list_sessions(self, **_: Any) -> list[dict[str, Any]]:
            raise RuntimeError("devin is down")

    watcher.devin = Boom()
    stats = watcher.cycle()
    assert stats["errors"] == 1
    assert store.get_meta("last_checked") is not None


def test_nothing_in_the_client_can_merge():
    """The one invariant the user asked for, enforced by grep."""
    from pathlib import Path

    source = Path("shared/github.py").read_text()
    assert "/merge" not in source
    assert "merge_method" not in source


def test_pr_url_falls_back_to_the_api_view_of_the_session():
    """A session that opens a PR but never reports it in structured output is
    still observable: the v3 session object lists its pull requests."""
    bare = session(11)
    bare["structured_output"] = {"issue_number": 11}
    bare["pull_requests"] = [{"url": "https://github.com/o/r/pull/4"}]
    assert task_update_from_session(bare)["pr_url"] == "https://github.com/o/r/pull/4"
    assert stage_for_session(bare) == "pr_open"


def test_durations_come_from_github_not_from_this_process_clock(tmp_path):
    """A control plane started after the work would otherwise report a day-long
    cycle as the few seconds between its own first two observations."""
    store, github, watcher = build(
        tmp_path,
        sessions=[
            session(1, "pr_opened_validated", output={"pr_url": "https://github.com/o/r/pull/5"})
        ],
        issues={"devin:ready": [issue(1)]},
    )
    github.pull_requests[5] = {
        "state": "open",
        "created_at": "2026-09-01T01:00:00Z",
        "head": {"ref": "devin/fix"},
    }
    watcher.cycle()
    task = store.get_task(1)
    assert task["pr_opened_at"] - task["detected_at"] == 3600


def test_run_cost_is_read_from_the_session():
    assert task_update_from_session(session(12, acus_consumed=3.5))["acus"] == 3.5
