#!/usr/bin/env python3
"""Fire a scan now, rather than waiting for the nightly schedule.

The automations API has no run-now endpoint. Its manual entry point is an
inbound-webhook trigger whose secret is issued once in the UI and returned as
``null`` by the API — which makes it impossible to set up programmatically. So
the scan also triggers on an issue labelled ``devin:scan``, and this opens one:
a request, not work. The scan closes it on arrival and ignores its body.

Labelling always succeeds, so it says nothing about whether Devin heard, and
the two ways of not being heard need different fixes: the fork is not in
Devin's GitHub access (a UI grant, no API), or the automation heard and skipped
the run because its daily cap is spent. Neither is visible from GitHub, so this
waits for the session it asked for and, failing that, says which one happened.

    REPO=you/superset GITHUB_TOKEN=... python -m scanner.run_now
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

from shared.config import config
from shared.devin import DevinClient, DevinError
from shared.github import GitHubClient, GitHubError

LABEL = os.environ.get("SCAN_LABEL", "devin:scan")
BODY = (
    "Requested by `scanner/run_now.py`. This issue is a trigger, not work — "
    "the scan closes it as soon as it starts.\n"
)
WAIT_SECONDS = 90

NOT_HEARD = """
✗ no scan session started within {waited}s, so the label reached no automation.

  Almost always Devin's access to the fork, which is granted in the UI only:

    1. open https://app.devin.ai/settings/integrations/github
    2. under GitHub, choose Configure / Manage repositories
    3. add {repo} to the repositories Devin may access, and save

  A fork you replaced is a different repository, so a fresh fork needs adding
  again. Then re-run `make scan`; the issue below stays valid, or label another.
"""

SKIPPED = """
✗ {name} heard the label and skipped the run: it is capped at {runs} runs per
  {window}, and this one would have been over it. Nothing is broken.

  Wait for the window, or raise limits.invocations.max_per_window in
  scanner/automation.json and re-run `make up` to apply it.
"""


def scan_session_ids(devin: DevinClient) -> set[str]:
    """Sessions the scan automation has spawned, newest run included."""
    return {
        str(s.get("session_id"))
        for s in devin.list_sessions(tags=[config.session_tag])
        if "role:scan" in (s.get("tags") or [])
    }


def skipped_run(devin: DevinClient, since: float) -> dict[str, Any] | None:
    """The scan automation, if it declined to run since ``since``.

    A skip is a rate limit, not a fault, and it is reported nowhere the
    operator looks — the issue is labelled, the automation is enabled, and no
    session exists.
    """
    try:
        automations = devin.list_automations()
    except DevinError:
        return None
    for automation in automations:
        if (automation.get("metadata") or {}).get("role") != "scanner":
            continue
        last = automation.get("last_invocation") or {}
        if last.get("status") == "skipped" and float(last.get("fired_at") or 0) >= since:
            return automation
    return None


def wait_for_scan(devin: DevinClient, before: set[str], seconds: float = WAIT_SECONDS) -> str | None:
    """The session id the label produced, or None if nothing started."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(5)
        try:
            if new := scan_session_ids(devin) - before:
                return sorted(new)[0]
        except DevinError:
            continue  # a flaky poll is not an answer
    return None


def main() -> int:
    repo, token = os.environ.get("REPO", ""), os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        print("REPO and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    devin = (
        DevinClient(config.devin_api_key, config.devin_org_id, config.devin_api_base)
        if config.devin_api_key and config.devin_org_id
        else None
    )
    before: set[str] = set()
    if devin:
        # Read before labelling: a session that starts in between is ours either
        # way. An unreadable list means no baseline, so do not claim one.
        try:
            before = scan_session_ids(devin)
        except DevinError as exc:
            print(f"  cannot read sessions ({exc}); requesting without watching", file=sys.stderr)
            devin = None

    github = GitHubClient(token, repo)
    requested_at = time.time()
    try:
        issue = github.create_issue("Run a scan", BODY)
        github.add_label(issue["number"], LABEL)
    except GitHubError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ scan requested: {issue['html_url']}")

    if devin is None:
        return 0
    print(f"  waiting up to {WAIT_SECONDS}s for the session it triggers")
    session_id = wait_for_scan(devin, before)
    if session_id is None:
        declined = skipped_run(devin, requested_at)
        if declined is None:
            print(NOT_HEARD.format(waited=WAIT_SECONDS, repo=repo), file=sys.stderr)
            return 1
        limit = (declined.get("limits") or {}).get("invocations") or {}
        hours = int(limit.get("window_seconds") or 0) // 3600
        print(
            SKIPPED.format(
                name=declined.get("name", "the scan automation"),
                runs=limit.get("max_per_window", "?"),
                window=f"{hours}h" if hours else "window",
            ),
            file=sys.stderr,
        )
        return 1
    print(f"✓ scanning: https://app.devin.ai/sessions/{session_id.removeprefix('devin-')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
