#!/usr/bin/env python3
"""Point the whole system at a fork, in one command:

1. fork Superset if the repo does not exist yet, and turn Issues on;
2. create the four labels the automations trigger on;
3. seed the classification registry's format spec;
4. create or update the playbook and both automations, scoped to this fork;
5. optionally fire the first scan.

Idempotent throughout, so a second run against a configured fork just prints
what it found — which is what lets compose run it on every start.

    REPO=you/superset python -m bootstrap --scan

Requires DEVIN_API_KEY (service user, Admin), DEVIN_ORG_ID and GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from bootstrap import automations, playbooks
from scanner import run_now
from shared.github import GitHubClient, GitHubError

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ".devin/classifications"

LABELS = [
    ("devin:ready", "0e8a16", "Queued for autonomous remediation"),
    ("devin:in-progress", "fbca04", "A Devin session is working on this"),
    ("devin:rejected", "b60205", "A human rejected this issue; its PR is closed"),
    ("devin:scan", "1d76db", "Requests an off-schedule scan; the scan closes the issue"),
]


def prepare_fork(github: GitHubClient, repo: str, upstream: str) -> list[str]:
    """Make the fork exist and be usable. Returns what could not be fixed."""
    try:
        info = github.repository()
    except GitHubError:
        print(f"+ forking {upstream} → {repo}; Superset is large, so this takes a few minutes")
        try:
            created = github.fork(upstream)
        except GitHubError as exc:
            return [f"cannot fork {upstream} to {repo}: {exc}"]

        # An account may hold one fork of an upstream, whatever it is called;
        # a second request quietly hands back the first, under its own name.
        landed = created.get("full_name") if isinstance(created, dict) else None
        if landed and landed != repo:
            owner = repo.split("/")[0]
            return [
                f"{owner} already forks {upstream} as {landed}. GitHub allows one fork of a "
                f"repo per account whatever it is named, so it returned that one and "
                f"{repo} was never created. Re-run with REPO={landed}, or rename or "
                f"delete {landed} first"
            ]
        # GitHub forks asynchronously and says nothing while it works, so say
        # it here: silence for minutes is indistinguishable from a hang.
        for attempt in range(1, 31):
            time.sleep(4)
            try:
                info = github.repository()
                break
            except GitHubError:
                if attempt % 5 == 0:
                    print(f"  still forking, {attempt * 4}s elapsed")
        else:
            return [f"{repo} did not appear after two minutes; re-run once GitHub finishes"]
        print(f"+ {repo} exists")

    # Write access is proved by attempting a write, never by reading
    # `permissions`: an App installation token reports every permission false
    # there while happily creating labels and issues.
    if info.get("has_issues"):
        return []
    try:
        github.enable_issues()
    except GitHubError as exc:
        return [
            f"Issues are disabled on {repo} and the token cannot enable them ({exc}); "
            f"tick Issues at https://github.com/{repo}/settings"
        ]
    print(f"+ Issues enabled on {repo}")
    return []


def banner(repo: str) -> str:
    """The one step with no API: granting Devin access to the fork.

    Missed, every trigger silently does nothing, so it is worth the box.
    """
    lines = [
        "ONE MANUAL STEP LEFT (skip it if you have already done it)",
        "",
        f"Devin needs access to {repo}, and only the UI can grant it:",
        "",
        "  1. open https://app.devin.ai/settings/integrations/github",
        "  2. under GitHub, choose Configure / Manage repositories",
        f"  3. add {repo} to the repositories Devin may access, and save",
        "",
        "Until then the scan cannot read the code and issue labels reach",
        "no automation, so the dashboard sits empty and looks broken.",
        "",
        "Then: http://localhost:8000 for the dashboard, and `make scan`",
        "in a second terminal to run a scan now rather than at 02:00 PT.",
    ]
    width = max(len(line) for line in lines)
    body = "\n".join(f"│ {line.ljust(width)} │" for line in lines)
    return f"\n┌─{'─' * width}─┐\n{body}\n└─{'─' * width}─┘\n"


def seed_registry(github: GitHubClient) -> None:
    """The registry's format spec and nothing else: what counts as a defect in
    this repo is the human's call, and the first scan proposes candidates."""
    if github.file_exists(f"{REGISTRY}/README.md"):
        print(f"= {REGISTRY}/README.md already present")
        return
    github.put_file(
        f"{REGISTRY}/README.md",
        (ROOT / "scanner" / "registry_seed.md").read_text(),
        "chore(devin): seed the classification registry",
    )
    print(f"+ {REGISTRY}/README.md committed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=os.environ.get("REPO", ""), help="owner/name of the fork")
    parser.add_argument(
        "--upstream",
        default=os.environ.get("UPSTREAM", "apache/superset"),
        help="repository to fork from if --repo does not exist yet",
    )
    parser.add_argument("--scan", action="store_true", help="fire the first scan when finished")
    args = parser.parse_args()

    if not args.repo:
        print("REPO (or --repo) is required, as owner/name", file=sys.stderr)
        return 2
    for name in ("DEVIN_API_KEY", "DEVIN_ORG_ID", "GITHUB_TOKEN"):
        if not os.environ.get(name):
            print(f"{name} is required; see the README", file=sys.stderr)
            return 2

    github = GitHubClient(os.environ["GITHUB_TOKEN"], args.repo)
    if problems := prepare_fork(github, args.repo, args.upstream):
        for problem in problems:
            print(f"✗ {problem}", file=sys.stderr)
        return 1
    print(f"✓ {args.repo} exists and has Issues enabled")

    for name, colour, description in LABELS:
        created = github.ensure_label(name, colour, description)
        print(f"{'+' if created else '='} label {name}")
    seed_registry(github)

    # Playbook first: the remediation automation's prompt references it by id.
    if code := playbooks.main([]):
        return code
    if code := automations.main(["--repo", args.repo]):
        return code

    code = run_now.main() if args.scan else 0

    # Last, and framed, because it is the one step no API can do and the
    # server's own logs start scrolling underneath it a second later.
    print(banner(args.repo))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
