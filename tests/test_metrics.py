from __future__ import annotations

from control_plane.metrics import compute, prometheus, report_markdown
from control_plane.store import Store


def seed(tmp_path) -> Store:
    store = Store(str(tmp_path / "m.db"))

    # verified and merged by a human
    store.upsert_task(1, classification="stale-dep", outcome="pr_opened_validated",
                      pr_url="u/1", pr_state="merged", validation_passed=1, attempts=1)
    for stage in ("detected", "dispatched", "running", "pr_open", "verified", "merged"):
        store.advance(1, stage)

    # PR opened but validation failed
    store.upsert_task(2, classification="stale-dep", outcome="pr_opened_validation_failed",
                      pr_url="u/2", validation_passed=0, failure_reason="verification_failed",
                      attempts=1)
    for stage in ("detected", "dispatched", "running", "pr_open"):
        store.advance(2, stage)

    # blocked — needed a human, never produced a PR
    store.upsert_task(3, classification="frontend-any", failure_reason="blocked_on_human", attempts=1)
    for stage in ("detected", "dispatched", "running"):
        store.advance(3, stage)

    # human rejected the finding itself
    store.upsert_task(4, classification="frontend-any", rejected=1, outcome="human_rejected", attempts=2)
    store.advance(4, "detected")
    return store


def test_funnel_shows_where_work_dies(tmp_path):
    metrics = compute(seed(tmp_path))
    assert metrics["funnel"]["detected"] == 4
    assert metrics["funnel"]["pr_open"] == 2
    assert metrics["funnel"]["merged"] == 1


def test_success_counts_verification_not_completion(tmp_path):
    metrics = compute(seed(tmp_path))
    # settled = 1 verified + 1 validation-failed + 1 rejected
    assert metrics["totals"]["settled"] == 3
    assert metrics["success_rate"] == 33.3


def test_autonomy_rate_includes_work_that_needed_a_human(tmp_path):
    """The bug this guards: measured over successes alone, autonomy is always
    100% and the blocked task — the clearest loss of autonomy — is invisible."""
    metrics = compute(seed(tmp_path))
    assert metrics["autonomy_rate"] == 25.0


def test_rejection_is_separate_from_failure(tmp_path):
    metrics = compute(seed(tmp_path))
    assert metrics["human_rejection_rate"] == 33.3
    assert "human_rejected" not in metrics["failure_taxonomy"]
    assert metrics["failure_taxonomy"] == {"verification_failed": 1, "blocked_on_human": 1}


def test_validation_mismatch_is_surfaced(tmp_path):
    store = seed(tmp_path)
    store.upsert_task(2, validate_command="pytest -q", validate_registry="npm audit --production")
    metrics = compute(store)
    assert metrics["validation_mismatches"] == 1
    assert metrics["per_class"]["stale-dep"]["validation_mismatch"] == 1


def test_filling_a_placeholder_is_not_a_mismatch(tmp_path):
    """Class files write the gate as a template and the session fills in the
    files it touched. Counting substitution as a mismatch made the metric read
    "almost every fix graded itself", which was noise."""
    store = seed(tmp_path)
    store.upsert_task(
        1,
        validate_registry="grep -q Licensed <files> && npm run type",
        validate_command="grep -q Licensed a.ts b.ts && npm run type",
    )
    assert compute(store)["validation_mismatches"] == 0


def test_attempts_are_averaged_over_dispatched_issues(tmp_path):
    """A detected issue nobody has worked yet is not an issue that took zero
    attempts; including it drags the average below one."""
    store = seed(tmp_path)
    store.upsert_task(9, classification="stale-dep")
    store.advance(9, "detected")
    metrics = compute(store)
    assert metrics["attempts_per_issue"] == 1.25
    assert metrics["reworked_issues"] == 1


def test_per_class_rates(tmp_path):
    metrics = compute(seed(tmp_path))
    assert metrics["per_class"]["stale-dep"]["success_rate"] == 50.0
    assert metrics["per_class"]["frontend-any"]["rejection_rate"] == 50.0


def test_cost_is_reported_per_merged_pr_when_measured(tmp_path):
    store = seed(tmp_path)
    for issue, acus in ((1, 12.0), (2, 10.0), (3, 8.0)):
        store.upsert_task(issue, acus=acus)
    metrics = compute(store)
    assert metrics["cost"]["run_acus"] == 30.0
    assert metrics["cost"]["acus_per_merged_pr"] == 30.0
    assert metrics["cost"]["acus_per_issue_detected"] == 7.5
    assert "## Cost" in report_markdown(metrics, store.tasks(), "o/r")
    assert "remediation_run_acus 30.0" in prometheus(metrics)


def test_cost_is_omitted_when_the_plan_exposes_no_consumption(tmp_path):
    """The consumption API is Enterprise-only; below it nothing is measured, and
    an unmeasured cost is not reported as zero, as a dash, or from config."""
    store = seed(tmp_path)
    metrics = compute(store)
    assert metrics["cost"] is None
    assert "## Cost" not in report_markdown(metrics, store.tasks(), "o/r")
    assert "acus" not in prometheus(metrics)


def test_rates_are_none_not_zero_when_there_is_no_data(tmp_path):
    """Zero would read as a real measurement of failure."""
    metrics = compute(Store(str(tmp_path / "empty.db")))
    assert metrics["success_rate"] is None
    assert metrics["cost"] is None


def test_prometheus_and_report_render(tmp_path):
    store = seed(tmp_path)
    metrics = compute(store)
    text = prometheus(metrics)
    assert 'remediation_funnel_total{stage="merged"} 1' in text
    assert "remediation_success_rate 33.3" in text

    report = report_markdown(metrics, store.tasks(), "juliaschell/superset")
    assert "## Methodology" in report
    assert "No PR is auto-merged" in report
    assert "blocked_on_human" in report
