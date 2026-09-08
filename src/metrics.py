"""Metrics, all derived from the event log and the task view.

Every definition here is a judgement about what counts as success, so each one
says what it measures and what it deliberately excludes.
"""

from __future__ import annotations

import statistics
import time
from typing import Any

from .store import STAGES, Store

VERIFIED_OUTCOME = "pr_opened_validated"


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def compute(store: Store, build_acus: float = 0.0, run_acus: float = 0.0) -> dict[str, Any]:
    tasks = store.tasks()
    total = len(tasks)
    funnel = store.furthest_stages()

    verified = [t for t in tasks if t.get("outcome") == VERIFIED_OUTCOME]
    rejected = [t for t in tasks if t.get("rejected")]
    merged = [t for t in tasks if t.get("pr_state") == "merged"]
    with_pr = [t for t in tasks if t.get("pr_url")]

    # Settled = we know how it ended. Anything still running is excluded from
    # rates rather than counted as a failure.
    settled = [t for t in tasks if t.get("outcome") or t.get("rejected")]

    # Autonomy: reached a verified PR without pulling a human in. Measured over
    # everything that settled *or* needed a human — over successes alone it
    # reads ~100% and means nothing.
    needed_human = [
        t for t in tasks if t.get("failure_reason") in ("blocked_on_human", "timed_out")
    ]
    autonomy_denominator = {t["issue_number"] for t in settled} | {
        t["issue_number"] for t in needed_human
    }
    autonomous = [t for t in verified if t.get("failure_reason") is None]

    time_to_pr = [
        t["pr_opened_at"] - t["detected_at"]
        for t in tasks
        if t.get("pr_opened_at") and t.get("detected_at")
    ]
    cycle = [
        t["settled_at"] - t["detected_at"]
        for t in tasks
        if t.get("settled_at") and t.get("detected_at")
    ]

    failure_taxonomy: dict[str, int] = {}
    for task in tasks:
        reason = task.get("failure_reason")
        if reason:
            failure_taxonomy[reason] = failure_taxonomy.get(reason, 0) + 1

    per_class: dict[str, dict[str, Any]] = {}
    for task in tasks:
        name = task.get("classification") or "unclassified"
        bucket = per_class.setdefault(
            name, {"volume": 0, "verified": 0, "rejected": 0, "validation_mismatch": 0}
        )
        bucket["volume"] += 1
        if task.get("outcome") == VERIFIED_OUTCOME:
            bucket["verified"] += 1
        if task.get("rejected"):
            bucket["rejected"] += 1
        if _validation_mismatch(task):
            bucket["validation_mismatch"] += 1
    for bucket in per_class.values():
        bucket["success_rate"] = _rate(bucket["verified"], bucket["volume"])
        bucket["rejection_rate"] = _rate(bucket["rejected"], bucket["volume"])

    last = store.get_meta("last_reconciled")
    last_reconciled_age = round(time.time() - float(last), 1) if last else None

    return {
        "totals": {
            "detected": total,
            "verified": len(verified),
            "merged": len(merged),
            "rejected": len(rejected),
            "settled": len(settled),
        },
        "funnel": {stage: funnel.get(stage, 0) for stage in STAGES},
        # Success is a *verified* PR, not "the session finished". A session
        # ending on a red build is not a success.
        "success_rate": _rate(len(verified), len(settled)),
        "autonomy_rate": _rate(len(autonomous), len(autonomy_denominator)),
        "human_rejection_rate": _rate(len(rejected), len(settled)),
        "verification_pass_rate": _rate(
            len([t for t in tasks if t.get("validation_passed") == 1]), len(with_pr)
        ),
        # A fix whose validation command differs from the one its class file
        # specifies. Not necessarily wrong — but it is the difference between a
        # curated gate and an improvised one, so it is never hidden.
        "validation_mismatches": len([t for t in tasks if _validation_mismatch(t)]),
        "attempts_per_issue": round(
            sum(t.get("attempts") or 0 for t in tasks) / total, 2
        )
        if total
        else 0,
        "reworked_issues": len([t for t in tasks if (t.get("attempts") or 0) > 1]),
        "median_time_to_pr_seconds": _median(time_to_pr),
        "median_cycle_seconds": _median(cycle),
        "failure_taxonomy": failure_taxonomy,
        "per_class": per_class,
        # ACUs are not exposed per session by the API for this org, so these are
        # recorded from the org usage page rather than measured here. Reported
        # as configured values so the payback figure is reproducible.
        "cost": {
            "build_acus": build_acus,
            "run_acus": run_acus,
            "acus_per_merged_pr": round(run_acus / len(merged), 2) if merged else None,
            "acus_per_issue_detected": round(run_acus / total, 2) if total else None,
            "source": "org usage page (per-session ACUs are not API-exposed)",
        },
        "last_reconciled_seconds_ago": last_reconciled_age,
    }


def _validation_mismatch(task: dict[str, Any]) -> bool:
    ran, registry = task.get("validate_command"), task.get("validate_registry")
    return bool(ran and registry and ran.strip() != registry.strip())


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(100.0 * numerator / denominator, 1)


def prometheus(metrics: dict[str, Any]) -> str:
    """Prometheus text exposition. Flat by design — this is a handful of
    findings a night, not a time series problem."""
    lines: list[str] = []

    def emit(name: str, value: Any, labels: str = "", help_text: str = "") -> None:
        if value is None:
            return
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name}{labels} {value}")

    for stage, count in metrics["funnel"].items():
        emit("remediation_funnel_total", count, f'{{stage="{stage}"}}')
    for key, value in metrics["totals"].items():
        emit("remediation_tasks_total", value, f'{{state="{key}"}}')
    emit("remediation_success_rate", metrics["success_rate"], help_text="Verified PRs / settled tasks")
    emit("remediation_autonomy_rate", metrics["autonomy_rate"], help_text="Verified with no human intervention")
    emit("remediation_rejection_rate", metrics["human_rejection_rate"])
    emit("remediation_verification_pass_rate", metrics["verification_pass_rate"])
    emit("remediation_validation_mismatches", metrics["validation_mismatches"])
    emit("remediation_attempts_per_issue", metrics["attempts_per_issue"])
    emit("remediation_median_time_to_pr_seconds", metrics["median_time_to_pr_seconds"])
    emit("remediation_last_reconciled_seconds", metrics["last_reconciled_seconds_ago"])
    for reason, count in metrics["failure_taxonomy"].items():
        emit("remediation_failures_total", count, f'{{reason="{reason}"}}')
    for name, bucket in metrics["per_class"].items():
        emit("remediation_class_volume", bucket["volume"], f'{{class="{name}"}}')
        emit("remediation_class_success_rate", bucket["success_rate"], f'{{class="{name}"}}')
        emit("remediation_class_rejection_rate", bucket["rejection_rate"], f'{{class="{name}"}}')
    return "\n".join(lines) + "\n"


def report_markdown(metrics: dict[str, Any], tasks: list[dict[str, Any]], repo: str) -> str:
    """The honest write-up. Methodology first, because the numbers mean nothing
    without the sample size."""
    t = metrics["totals"]
    cost = metrics["cost"]

    def pct(value: float | None) -> str:
        return f"{value}%" if value is not None else "n/a"

    lines = [
        f"# Remediation report — {repo}",
        "",
        "## Methodology",
        "",
        f"- Issues detected: **{t['detected']}** (sample size — read every rate below against it)",
        f"- Settled (outcome known): **{t['settled']}**; still in flight: **{t['detected'] - t['settled']}**",
        "- Success means a PR whose classification's validation command passed — not 'the session finished'.",
        "- No PR is auto-merged; the merged count reflects human decisions only.",
        f"- ACU figures are read from the org usage page ({cost['source']}).",
        "",
        "## Outcomes",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Success rate (verified / settled) | {pct(metrics['success_rate'])} |",
        f"| Autonomy rate (no human needed) | {pct(metrics['autonomy_rate'])} |",
        f"| Human rejection rate | {pct(metrics['human_rejection_rate'])} |",
        f"| Verification pass rate | {pct(metrics['verification_pass_rate'])} |",
        f"| Validation-command mismatches | {metrics['validation_mismatches']} |",
        f"| Attempts per issue | {metrics['attempts_per_issue']} |",
        f"| Issues reworked at least once | {metrics['reworked_issues']} |",
        f"| Median time to PR | {metrics['median_time_to_pr_seconds']}s |",
        f"| Merged by a human | {t['merged']} |",
        "",
        "## Funnel (cumulative — ever reached)",
        "",
        "| Stage | Count |",
        "|---|---|",
    ]
    lines += [f"| {stage} | {count} |" for stage, count in metrics["funnel"].items()]

    lines += ["", "## Why work failed", ""]
    if metrics["failure_taxonomy"]:
        lines += ["| Reason | Count |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(metrics["failure_taxonomy"].items())]
    else:
        lines.append("No failures recorded in this window.")

    lines += ["", "## Per classification", "", "| Class | Volume | Success | Rejected |", "|---|---|---|---|"]
    for name, bucket in sorted(metrics["per_class"].items()):
        lines.append(
            f"| {name} | {bucket['volume']} | {pct(bucket['success_rate'])} | {pct(bucket['rejection_rate'])} |"
        )

    lines += [
        "",
        "## Cost",
        "",
        f"- Build (planning + implementation sessions): **{cost['build_acus']} ACUs**",
        f"- Run: **{cost['run_acus']} ACUs**",
        f"- Per merged PR: **{cost['acus_per_merged_pr']}**",
        f"- Per issue detected: **{cost['acus_per_issue_detected']}**",
        "",
        "## Tasks",
        "",
        "| Issue | Class | Stage | Outcome | Attempts | PR |",
        "|---|---|---|---|---|---|",
    ]
    for task in tasks:
        lines.append(
            f"| #{task['issue_number']} | {task.get('classification') or '—'} | {task.get('stage')} |"
            f" {task.get('outcome') or '—'} | {task.get('attempts') or 0} | {task.get('pr_url') or '—'} |"
        )
    return "\n".join(lines) + "\n"
