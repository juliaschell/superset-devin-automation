"""Labelling an issue always succeeds; being heard is the part worth checking."""

from __future__ import annotations

from typing import Any

from scanner import run_now


class FakeDevin:
    """Returns each canned page of sessions in turn, then repeats the last."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages

    def list_sessions(self, **_: Any) -> list[dict[str, Any]]:
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


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
