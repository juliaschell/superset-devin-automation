"""The session report, which usually has to be parsed out of prose.

The platform will not attach a structured-output schema to an
automation-spawned session, so ``structured_output`` is null on every session
this system observes and the prompt asks for the JSON in the final message
instead. That makes the parsing here load-bearing rather than a fallback.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.devin import DevinClient, DevinError


class Client(DevinClient):
    """A client whose only I/O is a canned message list."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        super().__init__("key", "org")
        self._messages = messages
        self.calls = 0

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        self.calls += 1
        return self._messages


def session(**kwargs: Any) -> dict[str, Any]:
    return {"session_id": "s1", "structured_output": None, **kwargs}


def test_structured_output_wins_and_costs_no_request() -> None:
    client = Client([])
    out = client.report(session(structured_output={"outcome": "pr_opened_validated"}))
    assert out["outcome"] == "pr_opened_validated"
    assert client.calls == 0


def test_reads_the_fenced_block_in_the_final_message() -> None:
    client = Client(
        [
            {"message": "Working on #7."},
            {"message": 'Done.\n\n```json\n{"issue_number": 7, "outcome": "pr_opened_validated"}\n```'},
        ]
    )
    assert client.report(session()) == {"issue_number": 7, "outcome": "pr_opened_validated"}


def test_prefers_the_last_report_when_a_session_reported_twice() -> None:
    client = Client(
        [
            {"message": '```json\n{"outcome": "blocked"}\n```'},
            {"message": 'Retried.\n```json\n{"outcome": "pr_opened_validated"}\n```'},
        ]
    )
    assert client.report(session())["outcome"] == "pr_opened_validated"


def test_a_session_that_answered_in_prose_is_not_an_error() -> None:
    client = Client([{"message": "I could not work out what to do here."}])
    assert client.report(session()) == {}


def test_the_trigger_payload_is_not_mistaken_for_a_report() -> None:
    """The GitHub event that started the session arrives as fenced JSON in its
    first message, so a session still working has JSON that is not a report."""
    client = Client(
        [
            {"message": '```json\n{"action": "labeled", "issue": {"number": 14}}\n```'},
            {"message": "Reading the classification file."},
        ]
    )
    assert client.report(session()) == {}


def test_malformed_json_is_skipped_rather_than_raised() -> None:
    client = Client([{"message": '```json\n{"outcome": oops}\n```'}])
    assert client.report(session()) == {}


class ConsumptionClient(DevinClient):
    """A client whose only I/O is a canned consumption response."""

    def __init__(self, payload: Any, error: bool = False) -> None:
        super().__init__("key", "org")
        self.payload = payload
        self.error = error
        self.paths: list[str] = []

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.paths.append(path)
        if self.error:
            raise DevinError("403")
        return self.payload


def test_reads_the_session_cost_from_the_consumption_api() -> None:
    client = ConsumptionClient(
        {"total_acus": 4.25, "consumption_by_date": [{"date": 1, "acus": 4.25}]}
    )
    assert client.session_acus("s1") == 4.25
    assert client.paths == ["/v3/organizations/org/consumption/daily/sessions/devin-s1"]


def test_the_session_id_is_only_prefixed_once() -> None:
    client = ConsumptionClient({"total_acus": 1.0, "consumption_by_date": [{"acus": 1.0}]})
    client.session_acus("devin-s1")
    assert client.paths == ["/v3/organizations/org/consumption/daily/sessions/devin-s1"]


def test_no_consumption_rows_is_unknown_rather_than_free() -> None:
    client = ConsumptionClient({"total_acus": 0.0, "consumption_by_date": []})
    assert client.session_acus("s1") is None


def test_an_unreadable_billing_surface_does_not_break_the_cycle() -> None:
    assert ConsumptionClient(None, error=True).session_acus("s1") is None


class TimingOutTransport(DevinClient):
    """Every request times out at the transport layer, as a slow API does."""

    def __init__(self) -> None:
        super().__init__("key", "org")
        self.attempts = 0

        def fail(*_: Any, **__: Any) -> Any:
            self.attempts += 1
            raise httpx.ReadTimeout("timed out")

        self._client.request = fail  # type: ignore[method-assign]


def test_a_timeout_degrades_one_reading_rather_than_the_whole_cycle() -> None:
    """httpx raises its own exception type, so without translation a single slow
    request escapes past the callers that are meant to tolerate it."""
    client = TimingOutTransport()
    assert client.report(session()) == {}
    assert client.session_acus("s1") is None
    assert client.attempts == 4  # one retry per call
