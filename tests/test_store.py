from __future__ import annotations

from tracker.store import Store


def make(tmp_path) -> Store:
    return Store(str(tmp_path / "state.db"))


def test_upsert_reports_creation_once(tmp_path):
    store = make(tmp_path)
    assert store.upsert_task(1, title="a") is True
    assert store.upsert_task(1, title="a") is False


def test_upsert_overwrites_rather_than_merges(tmp_path):
    """Devin is authoritative; a local value never wins."""
    store = make(tmp_path)
    store.upsert_task(1, session_status="running")
    store.upsert_task(1, session_status="finished")
    assert store.get_task(1)["session_status"] == "finished"


def test_funnel_is_cumulative_not_current(tmp_path):
    """A task that reached 'merged' still counts in every earlier stage —
    otherwise the funnel shows a row of zeroes once work completes."""
    store = make(tmp_path)
    store.upsert_task(1)
    for stage in ("detected", "dispatched", "running", "pr_open", "verified", "merged"):
        store.advance(1, stage)
    stages = store.furthest_stages()
    assert stages["detected"] == 1
    assert stages["pr_open"] == 1
    assert stages["merged"] == 1


def test_funnel_shows_dropoff(tmp_path):
    store = make(tmp_path)
    for number in (1, 2, 3):
        store.upsert_task(number)
        store.advance(number, "detected")
        store.advance(number, "dispatched")
    store.advance(1, "pr_open")
    stages = store.furthest_stages()
    assert stages["dispatched"] == 3
    assert stages["pr_open"] == 1
    assert stages["merged"] == 0


def test_stage_never_moves_backwards(tmp_path):
    store = make(tmp_path)
    store.upsert_task(1)
    store.advance(1, "pr_open")
    store.advance(1, "running")
    assert store.get_task(1)["stage"] == "pr_open"


def test_a_caller_supplied_detection_time_wins(tmp_path):
    """GitHub knows when the issue was filed; this database only knows when it
    first looked, which on a fresh clone is minutes after a day-old cycle."""
    store = make(tmp_path)
    store.upsert_task(1, detected_at=1000.0)
    assert store.get_task(1)["detected_at"] == 1000.0


def test_a_known_timestamp_is_not_overwritten_by_the_clock(tmp_path):
    store = make(tmp_path)
    store.upsert_task(1, detected_at=1000.0, pr_opened_at=1200.0)
    store.advance(1, "pr_open")
    assert store.get_task(1)["pr_opened_at"] == 1200.0


def test_events_are_append_only_history(tmp_path):
    store = make(tmp_path)
    store.log("detected", issue_number=1, detail="x")
    store.log("dispatched", issue_number=1, session_id="s1")
    kinds = [e["kind"] for e in store.events()]
    assert "detected" in kinds and "dispatched" in kinds


def test_a_new_fork_starts_from_an_empty_database(tmp_path):
    """The database is a docker volume that outlives the fork it describes, so
    a run against a new fork would otherwise chart the old fork's funnel."""
    store = make(tmp_path)
    store.bind_repo("o/old")
    store.upsert_task(1, title="a")
    store.set_scans([{"session_id": "s"}])

    assert store.bind_repo("o/new") is True
    assert store.tasks() == []
    assert store.scans() == []
    assert store.get_meta("repo") == "o/new"
    assert [e["kind"] for e in store.events()] == ["repo_changed"]


def test_the_same_fork_keeps_its_history(tmp_path):
    store = make(tmp_path)
    store.bind_repo("o/r")
    store.upsert_task(1, title="a")
    assert store.bind_repo("o/r") is False
    assert len(store.tasks()) == 1
