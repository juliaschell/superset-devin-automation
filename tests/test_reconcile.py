from __future__ import annotations

from typing import Any

from src.reconcile import (
    Reconciler,
    failure_reason,
    issue_number_for,
    stage_for_session,
    task_update_from_session,
)
from src.store import Store


class FakeConfig:
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
    return store, github, Reconciler(store, FakeDevin(sessions or []), github, FakeConfig())


def test_detection_then_dispatch_is_recorded_once(tmp_path):
    store, _, reconciler = build(
        tmp_path,
        sessions=[session(1, "pr_opened_validated", output={"pr_url": "https://github.com/o/r/pull/5"})],
        issues={"devin:ready": [issue(1)]},
    )
    reconciler.cycle()
    reconciler.cycle()
    assert len([e for e in store.events() if e["kind"] == "detected"]) == 1
    assert store.get_task(1)["attempts"] == 1
    assert store.get_task(1)["stage"] == "verified"


def test_second_session_on_one_issue_counts_as_rework(tmp_path):
    """A human requesting changes fires a new session; that is attempt 2 of the
    same issue, not a duplicate task."""
    store, _, reconciler = build(tmp_path, issues={"devin:ready": [issue(1)]})
    reconciler.devin = FakeDevin([session(1, session_id="a")])
    reconciler.cycle()
    reconciler.devin = FakeDevin([session(1, session_id="b")])
    reconciler.cycle()
    assert store.get_task(1)["attempts"] == 2
    assert store.get_task(1)["session_id"] == "b"


def test_two_sessions_on_one_issue_do_not_inflate_attempts_per_poll(tmp_path):
    """The API returns concurrent sessions in no particular order, so counting
    "this session differs from the one on the row" charges a fresh attempt every
    cycle. Attempts are distinct sessions, however often they are seen."""
    store, _, reconciler = build(tmp_path, issues={"devin:ready": [issue(1)]})
    reconciler.devin = FakeDevin([session(1, session_id="a"), session(1, session_id="b")])
    for _ in range(5):
        reconciler.cycle()
    assert store.get_task(1)["attempts"] == 2


def test_an_unattached_session_is_logged_once_not_once_per_cycle(tmp_path):
    """Scan sessions never carry an issue number, so logging them on every poll
    would bury the event log within an hour."""
    store, _, reconciler = build(tmp_path)
    reconciler.devin = FakeDevin([session(None, session_id="scan")])
    for _ in range(4):
        reconciler.cycle()
    assert len([e for e in store.events() if e["kind"] == "session_unattached"]) == 1


def test_rejected_issue_is_cleaned_up_and_distinguished_from_failure(tmp_path):
    store, github, reconciler = build(
        tmp_path,
        sessions=[session(1, "pr_opened_validated", output={"pr_url": "https://github.com/o/r/pull/5"})],
        issues={"devin:ready": [issue(1)], "devin:rejected": [issue(1, "devin:rejected")]},
    )
    reconciler.cycle()
    task = store.get_task(1)
    assert task["rejected"] == 1
    assert task["outcome"] == "human_rejected"
    assert task["failure_reason"] is None  # rejection is not a remediation failure
    assert github.calls == ["close_pr:5", "delete_branch:devin/fix", "close_issue:1"]


def test_cleanup_is_idempotent(tmp_path):
    store, github, reconciler = build(
        tmp_path, issues={"devin:rejected": [issue(1, "devin:rejected")]}
    )
    reconciler.cycle()
    reconciler.cycle()
    assert github.calls.count("close_issue:1") == 1


def test_a_broken_api_does_not_kill_the_loop(tmp_path):
    """A cycle that throws must still stamp freshness data, so the dashboard can
    say it is stale instead of quietly serving old numbers."""
    store, _, reconciler = build(tmp_path)

    class Boom:
        def list_sessions(self, **_: Any) -> list[dict[str, Any]]:
            raise RuntimeError("devin is down")

    reconciler.devin = Boom()
    stats = reconciler.cycle()
    assert stats["errors"] == 1
    assert store.get_meta("last_reconciled") is not None


def test_nothing_in_the_client_can_merge():
    """The one invariant the user asked for, enforced by grep."""
    from pathlib import Path

    source = Path("src/github.py").read_text()
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


def test_run_cost_is_read_from_the_session():
    assert task_update_from_session(session(12, acus_consumed=3.5))["acus"] == 3.5
