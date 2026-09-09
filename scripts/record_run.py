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
        # Bake each session's report into the frame. Offline replay has no API
        # to ask, and ``structured_output`` arrives null — so if the report is
        # not resolved here, the recording remembers only that a session ran.
        sessions = [{**s, "structured_output": devin.report(s)} for s in sessions]
        for session in sessions:
            out = session.get("structured_output") or {}
            url = out.get("pr_url") if isinstance(out, dict) else None
            number = pr_number_from_url(url) if url else None
            if number:
                pull_requests[str(number)] = github.get_pull_request(number)
        frames.append(
            {
                "captured_at": time.time(),
                "sessions": sessions,
                "issues": _slim_issues(issues),
                "pull_requests": pull_requests,
            }
        )
        Path(args.out).write_text(json.dumps({"repo": args.repo, "frames": frames}, indent=1))
        print(f"frame {len(frames)}: {len(sessions)} sessions, {len(issues)} issues")
        time.sleep(args.interval)
    return 0


def _slim_issues(issues: list[dict]) -> list[dict]:
    """Keep only the fields the reconciler reads. Recordings are committed, so
    they should not carry the whole GitHub payload."""
    return [
        {
            "number": i["number"],
            "title": i.get("title", ""),
            "html_url": i.get("html_url", ""),
            "state": i.get("state"),
            "labels": [{"name": lab["name"]} for lab in i.get("labels", [])],
        }
        for i in issues
    ]


if __name__ == "__main__":
    raise SystemExit(main())
