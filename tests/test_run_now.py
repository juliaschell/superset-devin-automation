"""Labelling an issue always succeeds; being heard is the part worth checking."""

from __future__ import annotations

from typing import Any

from scanner import run_now


def automation(status: str, fired_at: float, role: str = "scanner") -> dict[str, Any]:
    return {
        "name": "Superset — nightly scan",
        "metadata": {"role": role},
        "limits": {"invocations": {"max_per_window": 12, "window_seconds": 86400}},
        "last_invocation": {"status": status, "fired_at": fired_at},
    }


class FakeDevin:
    """Returns each canned page of sessions in turn, then repeats the last."""

    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        automations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages
        self.automations = automations or []

    def list_sessions(self, **_: Any) -> list[dict[str, Any]]:
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

    def list_automations(self) -> list[dict[str, Any]]:
        return self.automations


def scan(session_id: str) -> dict[str, Any]:
    return {"session_id": session_id, "tags": ["superset-remediation", "role:scan"]}


def test_the_session_the_label_started_is_the_new_one(monkeypatch):
    """An org with older scans in it must not read as a session that just
    started, or the check would pass whether or not Devin heard."""
    monkeypatch.setattr(run_now.time, "sleep", lambda _: None)
    devin = FakeDevin([[scan("devin-old")], [scan("devin-old"), scan("devin-new")]])
    assert run_now.wait_for_scan(devin, {"devin-old"}, seconds=30) == "devin-new"


def test_nothing_starting_is_reported_rather_than_waited_on_forever(monkeypatch):
    monkeypatch.setattr(run_now.time, "sleep", lambda _: None)
    devin = FakeDevin([[scan("devin-old")]])
    assert run_now.wait_for_scan(devin, {"devin-old"}, seconds=0.2) is None


def test_a_remediation_session_is_not_mistaken_for_a_scan(monkeypatch):
    """Both carry the project tag, and a remediation starting from an earlier
    issue would otherwise report the scan as running."""
    monkeypatch.setattr(run_now.time, "sleep", lambda _: None)
    remediate = {"session_id": "devin-fix", "tags": ["superset-remediation", "role:remediate"]}
    assert run_now.wait_for_scan(FakeDevin([[remediate]]), set(), seconds=0.2) is None


def test_a_run_the_cap_declined_is_not_reported_as_a_missing_grant():
    """The two silences look identical from GitHub and have opposite fixes:
    one is a UI grant, the other is waiting or raising the cap."""
    devin = FakeDevin([[]], [automation("skipped", fired_at=200.0)])
    declined = run_now.skipped_run(devin, since=100.0)
    assert declined is not None
    assert declined["limits"]["invocations"]["max_per_window"] == 12


def test_an_old_skip_says_nothing_about_this_request():
    devin = FakeDevin([[]], [automation("skipped", fired_at=50.0)])
    assert run_now.skipped_run(devin, since=100.0) is None


def test_a_successful_run_is_not_a_skip():
    devin = FakeDevin([[]], [automation("succeeded", fired_at=200.0)])
    assert run_now.skipped_run(devin, since=100.0) is None
