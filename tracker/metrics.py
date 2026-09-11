"""Metrics, derived from the event log and the task cache.

What counts as success is a judgement, so each definition says what it excludes
as well as what it counts.
"""

from __future__ import annotations

import re
import statistics
import time
from typing import Any

from .store import STAGES, Store

VERIFIED_OUTCOME = "pr_opened_validated"
# A class file writes its validate command with placeholders for the paths a
# fix touches: `grep ... <files>`, `<file>`, `<scope>`.
PLACEHOLDER = re.compile(r"<[a-z_]+>")


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def compute(store: Store) -> dict[str, Any]:
    tasks = store.tasks()
    total = len(tasks)
    funnel = store.furthest_stages()

    verified = [t for t in tasks if t.get("outcome") == VERIFIED_OUTCOME]
    rejected = [t for t in tasks if t.get("rejected")]
    merged = [t for t in tasks if t.get("pr_state") == "merged"]
    with_pr = [t for t in tasks if t.get("pr_url")]
    sent_back = [t for t in with_pr if (t.get("changes_requested") or 0) > 0]

    # Settled = we know how it ended. Anything still running is excluded from
    # rates rather than counted as a failure.
    settled = [t for t in tasks if t.get("outcome") or t.get("rejected")]

    # Autonomy: a verified PR with no human pulled in. The denominator is
    # everything that settled *or* needed a human — over successes alone it
    # reads ~100% and means nothing.
    needed_human = [
        t for t in tasks if t.get("failure_reason") in ("blocked_on_human", "timed_out")
    ]
    autonomy_denominator = {t["issue_number"] for t in settled} | {
        t["issue_number"] for t in needed_human
    }
    autonomous = [t for t in verified if t.get("failure_reason") is None]

    attempts = [t["attempts"] for t in tasks if (t.get("attempts") or 0) > 0]

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

    # Reported only where the consumption API measured it (Enterprise only).
    # Below that plan no task carries ACUs and the section disappears, rather
    # than showing a zero that would read as free work.
    run = round(sum(t.get("acus") or 0.0 for t in tasks), 2) or None
    cost = (
        {
            "run_acus": run,
            "acus_per_merged_pr": round(run / len(merged), 2) if merged else None,
            "acus_per_issue_detected": round(run / total, 2) if total else None,
        }
        if run
        else None
    )

    last = store.get_meta("last_checked")
    last_checked_age = round(time.time() - float(last), 1) if last else None

    return {
        "totals": {
            "detected": total,
            "verified": len(verified),
            "merged": len(merged),
            "rejected": len(rejected),
            "settled": len(settled),
        },
        "funnel": {stage: funnel.get(stage, 0) for stage in STAGES},
        # A verified PR, not a finished session: ending on a red build is not
        # a success.
        "success_rate": _rate(len(verified), len(settled)),
        "autonomy_rate": _rate(len(autonomous), len(autonomy_denominator)),
        "human_rejection_rate": _rate(len(rejected), len(settled)),
        "verification_pass_rate": _rate(
            len([t for t in tasks if t.get("validation_passed") == 1]), len(with_pr)
        ),
        # Fixes graded by a command their class file did not specify.
        "validation_mismatches": len([t for t in tasks if _validation_mismatch(t)]),
        # Over dispatched issues only: an issue nobody has worked yet did not
        # take zero attempts.
        "attempts_per_issue": round(sum(attempts) / len(attempts), 2)
        if attempts
        else 0,
        "reworked_issues": len([t for t in tasks if (t.get("attempts") or 0) > 1]),
        # A PR a human read and sent back. Nothing in this system reacts to it
        # — it is the honest measure of how often a first attempt is not good
        # enough, over the PRs a human has had the chance to judge.
        "changes_requested_prs": len(sent_back),
        "prs_opened": len(with_pr),
        "changes_requested_rate": _rate(len(sent_back), len(with_pr)),
        "median_time_to_pr_seconds": _median(time_to_pr),
        "median_cycle_seconds": _median(cycle),
        "failure_taxonomy": failure_taxonomy,
        "per_class": per_class,
        "cost": cost,
        "last_checked_seconds_ago": last_checked_age,
    }


def _validation_mismatch(task: dict[str, Any]) -> bool:
    """Did the fix run a gate other than the one its class file specifies?

    Filling in a placeholder (``grep ... <files>``) is the command being used as
    intended. Anything else is a session grading its own homework.
    """
    ran, registry = task.get("validate_command"), task.get("validate_registry")
    if not ran or not registry:
        return False
    pattern = ".+".join(
        re.escape(part) for part in PLACEHOLDER.split(registry.strip())
    )
    return re.fullmatch(pattern, ran.strip(), re.DOTALL) is None


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
    emit(
        "remediation_changes_requested_rate",
        metrics["changes_requested_rate"],
        help_text="PRs a human sent back / PRs opened",
    )
    emit("remediation_median_time_to_pr_seconds", metrics["median_time_to_pr_seconds"])
    emit("remediation_last_check_seconds", metrics["last_checked_seconds_ago"])
    # Absent rather than zero when the plan does not expose consumption.
    if metrics["cost"]:
        emit("remediation_run_acus", metrics["cost"]["run_acus"])
        emit("remediation_acus_per_merged_pr", metrics["cost"]["acus_per_merged_pr"])
    for reason, count in metrics["failure_taxonomy"].items():
        emit("remediation_failures_total", count, f'{{reason="{reason}"}}')
    for name, bucket in metrics["per_class"].items():
        emit("remediation_class_volume", bucket["volume"], f'{{class="{name}"}}')
        emit("remediation_class_success_rate", bucket["success_rate"], f'{{class="{name}"}}')
        emit("remediation_class_rejection_rate", bucket["rejection_rate"], f'{{class="{name}"}}')
    return "\n".join(lines) + "\n"


def report_markdown(
    metrics: dict[str, Any],
    tasks: list[dict[str, Any]],
    repo: str,
    waiting: list[dict[str, Any]] | None = None,
) -> str:
    """The write-up, methodology first: the rates mean nothing without the
    sample size they were taken over."""
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
        "- Cost is reported only where the consumption API measured it, which "
        "requires an Enterprise account; otherwise there is no cost section.",
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
        f"| PRs a human sent back | {metrics['changes_requested_prs']}"
        f" of {metrics['prs_opened']} ({pct(metrics['changes_requested_rate'])}) |",
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

    lines += ["", "## Waiting on a human", ""]
    if waiting:
        lines += ["| PR | What | Your move | Open for |", "|---|---|---|---|"]
        for w in waiting:
            what = " · ".join(filter(None, [w["kind"], w["what"]]))
            lines.append(
                f"| [{w['title']}]({w['url']}) | {what} | {w['do']} | {w['waiting_for']} |"
            )
    else:
        lines.append("Nothing open. Every PR this loop produced has been decided.")

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

    if cost:
        lines += [
            "",
            "## Cost",
            "",
            f"- Run: **{cost['run_acus']} ACUs**",
            f"- Per merged PR: **{cost['acus_per_merged_pr']} ACUs**",
            f"- Per issue detected: **{cost['acus_per_issue_detected']} ACUs**",
        ]

    lines += [
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
