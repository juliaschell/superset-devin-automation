#!/usr/bin/env python3
"""Fire a scan now, rather than waiting for the nightly schedule.

The automations API has no run-now endpoint. Its manual entry point is an
inbound-webhook trigger whose secret is issued once in the UI and returned as
``null`` by the API — which makes it impossible to set up programmatically. So
the scan also triggers on an issue labelled ``devin:scan``, and this opens one:
a request, not work. The scan closes it on arrival and ignores its body.

    REPO=you/superset GITHUB_TOKEN=... python -m scanner.run_now
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.github import GitHubClient, GitHubError  # noqa: E402

LABEL = os.environ.get("SCAN_LABEL", "devin:scan")
BODY = (
    "Requested by `scanner/run_now.py`. This issue is a trigger, not work — "
    "the scan closes it as soon as it starts.\n"
)


def main() -> int:
    repo, token = os.environ.get("REPO", ""), os.environ.get("GITHUB_TOKEN", "")
    if not repo or not token:
        print("REPO and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    github = GitHubClient(token, repo)
    try:
        issue = github.create_issue("Run a scan", BODY)
        github.add_label(issue["number"], LABEL)
    except GitHubError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    print(f"✓ scan requested: {issue['html_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
