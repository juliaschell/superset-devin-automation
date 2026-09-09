"""The automation definitions are infrastructure, so they get tests too.

These are cheap structural checks against the payloads we POST — they catch the
mistakes that otherwise only surface as a 400 from the API or, worse, as an
automation that exists and never fires.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bootstrap.automations import render as _render

PLAYBOOKS = {"Superset remediation": "playbook-test"}


def render(role: str, repo: str, max_issues: int) -> dict:
    return _render(role, repo, max_issues, PLAYBOOKS)

ROOT = Path(__file__).resolve().parent.parent
REPO = "juliaschell/superset"


def test_no_placeholder_survives_rendering():
    for role in ("scanner", "remediator"):
        assert "{{" not in json.dumps(render(role, REPO, 8))


def test_scan_is_scheduled_and_manually_runnable():
    spec = render("scanner", REPO, 8)
    triggers = spec["triggers"]
    assert any(t["event_type"] == "schedule:recurring" for t in triggers)
    # There is no run-now endpoint (POST .../run is a 404) and the webhook
    # trigger's secret is UI-only, so the manual entry point is a label the
    # GitHub token can apply. Losing it loses the manual path.
    manual = [t for t in triggers if t["event_type"] == "github:issues"]
    assert manual and "devin:scan" in json.dumps(manual)


def test_remediator_has_both_entry_points():
    """Label for new work, changes_requested for rework. The second is the
    human-rework path and it is easy to lose in an edit."""
    triggers = {t["event_type"]: t for t in render("remediator", REPO, 8)["triggers"]}
    assert set(triggers) == {"github:issues", "github:pull_request_review"}
    assert "devin:ready" in json.dumps(triggers["github:issues"])
    assert "changes_requested" in json.dumps(triggers["github:pull_request_review"])


def test_every_trigger_is_scoped_to_our_fork():
    """Without this an org-level automation fires on every connected repo."""
    for role in ("scanner", "remediator"):
        for trigger in render(role, REPO, 8)["triggers"]:
            if trigger["event_type"].startswith("github:"):
                assert REPO in json.dumps(trigger["conditions"])


def test_egress_is_an_explicit_allowlist():
    """Omitting net_policy blocks everything; a wildcard would defeat the
    control that makes prompt injection on a public repo survivable."""
    for role in ("scanner", "remediator"):
        policy = render(role, REPO, 8)["session_settings"]["net_policy"]
        hosts = json.dumps(policy)
        assert "git-manager.devin.ai" in hosts
        assert "*" not in hosts.replace('"*/*"', "")


def test_sessions_are_tagged_for_watching():
    """The watcher finds work by tag; an untagged automation is invisible."""
    for role in ("scanner", "remediator"):
        spec = render(role, REPO, 8)
        for action in spec["actions"]:
            if action["type"] == "start_session":
                assert "superset-remediation" in action["session"]["tags"]


def test_acu_limits_are_set():
    for role in ("scanner", "remediator"):
        assert render(role, REPO, 8)["limits"]["max_acu_limit"] > 0


def test_prompts_forbid_merging():
    """The invariant is enforced by permissions and by the prompt; this checks the
    prompt half has not been edited away."""
    for role in ("scanner", "remediator"):
        text = (ROOT / role / "prompt.md").read_text().lower()
        assert "never merge" in text or "do not merge" in text


def test_structured_output_schema_is_stated_in_the_prompt():
    """The API rejects a schema on the spawned session, so the shape is requested
    in the prompt instead. That makes it advisory, which is why the watcher
    treats every structured field as optional."""
    spec = render("remediator", REPO, 8)
    action = next(a for a in spec["actions"] if a["type"] == "start_session")
    assert "structured_output_schema" not in action["session"]
    assert "outcome" in action["prompt"]


def test_the_remediator_is_bound_to_its_playbook():
    """`session.playbook_id` is read-only on the API: it derives from an
    `@playbook:<id>` token at the head of the prompt. Losing the token silently
    unbinds the procedure and the automation still runs."""
    prompt = render("remediator", REPO, 8)["actions"][0]["prompt"]
    assert prompt.startswith("@playbook:playbook-test")


def test_a_missing_playbook_fails_loudly():
    with pytest.raises(KeyError):
        _render("remediator", REPO, 8, {})
