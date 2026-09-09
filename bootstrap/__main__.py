#!/usr/bin/env python3
"""Point the whole system at a fork, in one command.

Everything here is idempotent, so running it against an already-configured fork
is a no-op that prints what it found. What it does, in order:

1. forks Superset if the repo does not exist yet, and turns Issues on — forks
   have them off by default, and the whole pipeline files issues;
2. creates the four labels the automations trigger on;
3. seeds an empty classification registry, so the first scan has a format to
   follow and proposes classes instead of inventing one;
4. creates or updates the remediation playbook and both automations over REST,
   scoped to this fork;
5. optionally fires the first scan.

    REPO=you/superset python -m bootstrap --scan

Requires DEVIN_API_KEY (service user, Admin), DEVIN_ORG_ID and GITHUB_TOKEN.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bootstrap import automations, playbooks  # noqa: E402
from scanner import run_now  # noqa: E402
from shared.github import GitHubClient, GitHubError  # noqa: E402

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
        print(f"+ forking {upstream} → {repo}")
        try:
            github.fork(upstream)
        except GitHubError as exc:
            return [f"cannot fork {upstream} to {repo}: {exc}"]
        for _ in range(30):  # GitHub forks asynchronously
            time.sleep(4)
            try:
                info = github.repository()
                break
            except GitHubError:
                continue
        else:
            return [f"{repo} did not appear after forking; re-run once GitHub finishes"]

    # Write access is established by attempting a write, not by reading
    # `permissions`: a GitHub App installation token reports every permission
    # false there while happily creating labels and issues.
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


def seed_registry(github: GitHubClient) -> None:
    """Give a fresh fork the registry's format spec and nothing else.

    Deliberately no starter classes: what counts as a defect here is the
    human's call, and the first scan proposes candidates to accept or decline.
    """
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

    print(
        f"\nOne thing left that no API offers: connect {args.repo} to Devin\n"
        "(https://app.devin.ai/settings) so GitHub events reach the automations.\n"
    )

    if args.scan:
        return run_now.main()
    print("Then: `make scan` to run one now, or wait for the nightly. `make run` for the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
