#!/usr/bin/env python3
"""Record a live run so ``make demo`` can replay it with no credentials.

Polls the same two APIs the reconciler polls and appends a frame per cycle. The
result is a recording of what actually happened — the demo does not invent
outcomes, and anything a reviewer sees offline occurred live.

    python -m scripts.record_run --minutes 45
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.devin import DevinClient  # noqa: E402
from src.github import GitHubClient, pr_number_from_url  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "demo" / "recorded_run.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--repo", default=os.environ.get("REPO", "juliaschell/superset"))
    parser.add_argument("--tag", default=os.environ.get("SESSION_TAG", "superset-remediation"))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    devin = DevinClient(os.environ["DEVIN_API_KEY"], os.environ.get("DEVIN_ORG_ID", ""))
    github = GitHubClient(os.environ["GITHUB_TOKEN"], args.repo)

    frames: list[dict] = []
    deadline = time.time() + args.minutes * 60
    while time.time() < deadline:
        sessions = devin.list_sessions(tags=[args.tag])
        issues = [
            i
            for i in github.issues_with_label("devin:ready", state="all")
            + github.issues_with_label("devin:rejected", state="all")
            if not i.get("pull_request")
        ]
        pull_requests = {}
        # Bake each session's report and cost into the frame. Offline replay
        # has no API to ask, and both arrive empty on the session object — so if
        # they are not resolved here, the recording remembers only that a
        # session ran.
        sessions = [
            {
                **s,
                "structured_output": devin.report(s),
                "acus_consumed": devin.session_acus(str(s.get("session_id") or "")),
            }
            for s in sessions
        ]
        for session in sessions:
            out = session.get("structured_output") or {}
            url = out.get("pr_url") if isinstance(out, dict) else None
            number = pr_number_from_url(url) if url else None
            if number:
                pull_requests[str(number)] = slim_pull_request(github.get_pull_request(number))
        frames.append(
            {
                "captured_at": time.time(),
                "sessions": [slim_session(s) for s in sessions],
                "issues": [slim_issue(i) for i in issues],
                "pull_requests": pull_requests,
            }
        )
        Path(args.out).write_text(json.dumps({"repo": args.repo, "frames": frames}, indent=1))
        print(f"frame {len(frames)}: {len(sessions)} sessions, {len(issues)} issues")
        time.sleep(args.interval)
    return 0


# Recordings are committed to a public repo, so a frame carries only the fields
# the reconciler reads. That keeps the file reviewable, and it means nothing
# incidental in a GitHub or Devin payload ships along with it.


def slim_issue(issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue.get("title", ""),
        "html_url": issue.get("html_url", ""),
        "state": issue.get("state"),
        "labels": [{"name": lab["name"]} for lab in issue.get("labels", [])],
    }


def slim_session(session: dict) -> dict:
    return {
        key: session.get(key)
        for key in (
            "session_id",
            "title",
            "url",
            "status",
            "status_detail",
            "created_at",
            "updated_at",
            "tags",
            "pull_requests",
            "structured_output",
            "acus_consumed",
        )
    }


def slim_pull_request(pr: dict) -> dict:
    return {
        "number": pr.get("number"),
        "html_url": pr.get("html_url"),
        "state": pr.get("state"),
        "merged_at": pr.get("merged_at"),
        "head": {"ref": (pr.get("head") or {}).get("ref")},
    }


if __name__ == "__main__":
    raise SystemExit(main())
