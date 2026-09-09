"""Offline replay: the reviewer runs ``make demo`` with no credentials.

This is not a simulator that invents outcomes. It replays a **recorded real
run** — the issues that were actually filed and the session objects the API
actually returned — frame by frame, so the dashboard fills in the way it did
live. Anything you see in demo mode happened.

Recording is produced by ``scripts/record_run.py`` against a live run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RECORDING = Path(__file__).resolve().parent.parent / "demo" / "recorded_run.json"


def _load() -> dict[str, Any]:
    if not RECORDING.exists():
        return {"frames": []}
    return json.loads(RECORDING.read_text())


class _Replay:
    """Frames advance one per reconcile cycle, then hold on the last frame."""

    _state: dict[str, Any] = {"index": 0, "data": None}

    @property
    def data(self) -> dict[str, Any]:
        if self._state["data"] is None:
            self._state["data"] = _load()
        return self._state["data"]

    @property
    def frame(self) -> dict[str, Any]:
        frames = self.data.get("frames") or [{}]
        index = min(self._state["index"], len(frames) - 1)
        return frames[index]

    def advance(self) -> None:
        frames = self.data.get("frames") or []
        if self._state["index"] < len(frames) - 1:
            self._state["index"] += 1


class ReplayDevin(_Replay):
    def list_sessions(self, tags: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sessions = self.frame.get("sessions", [])
        # Devin is read after GitHub in a cycle, so advancing here keeps one
        # frame per cycle.
        self.advance()
        return sessions

    def report(self, session: dict[str, Any]) -> dict[str, Any]:
        # The recorder resolved this from the session's messages while it still
        # had an API to ask; offline it is just a field in the frame.
        out = session.get("structured_output")
        return out if isinstance(out, dict) else {}

    def get_session(self, session_id: str) -> dict[str, Any]:
        for session in self.frame.get("sessions", []):
            if session.get("session_id") == session_id:
                return session
        return {}


class ReplayGitHub(_Replay):
    def issues_with_label(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        return [
            issue
            for issue in self.frame.get("issues", [])
            if label in [lab["name"] for lab in issue.get("labels", [])]
        ]

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return self.frame.get("pull_requests", {}).get(str(number), {"state": "open"})

    # Cleanup is a no-op offline: nothing to close, and the recording already
    # contains whatever the real run did.
    def close_pull_request(self, number: int) -> None:
        return None

    def close_issue(self, issue_number: int) -> None:
        return None

    def delete_branch(self, branch: str) -> None:
        return None

    def add_label(self, issue_number: int, label: str) -> None:
        return None

    def comment(self, issue_number: int, body: str) -> None:
        return None
