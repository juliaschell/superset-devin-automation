"""Bootstrap is the first thing anyone runs, so its failure modes are tested.

The interesting cases are the ones that would otherwise fail silently much
later: a fork with Issues turned off files nothing, and a registry overwritten
on a re-run would destroy human decisions.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts import bootstrap
from src.github import GitHubError


class FakeGitHub:
    def __init__(
        self,
        info: dict[str, Any] | None,
        files: set[str] | None = None,
        can_write: bool = True,
    ) -> None:
        self.info = info
        self.files = files or set()
        self.can_write = can_write
        self.calls: list[str] = []

    def repository(self) -> dict[str, Any]:
        if self.info is None:
            raise GitHubError("404")
        return self.info

    def enable_issues(self) -> None:
        if not self.can_write:
            raise GitHubError("403")
        self.calls.append("enable_issues")
        self.info = {**(self.info or {}), "has_issues": True}

    def fork(self, upstream: str) -> None:
        self.calls.append(f"fork:{upstream}")
        self.info = {"has_issues": False}

    def file_exists(self, path: str) -> bool:
        return path in self.files

    def put_file(self, path: str, content: str, message: str) -> None:
        self.calls.append(f"put:{path}")
        self.files.add(path)


def test_issues_are_turned_on_rather_than_reported():
    """Forks have Issues off, and every issue the pipeline files would 404."""
    github = FakeGitHub({"has_issues": False})
    assert bootstrap.prepare_fork(github, "me/superset", "apache/superset") == []
    assert "enable_issues" in github.calls


def test_issues_that_cannot_be_enabled_stop_the_run():
    github = FakeGitHub({"has_issues": False}, can_write=False)
    problems = bootstrap.prepare_fork(github, "me/superset", "apache/superset")
    assert problems and "settings" in problems[0]


def test_a_missing_repo_is_forked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    github = FakeGitHub(None)
    assert bootstrap.prepare_fork(github, "me/superset", "apache/superset") == []
    assert github.calls[:2] == ["fork:apache/superset", "enable_issues"]


def test_an_existing_registry_is_never_overwritten():
    """It holds human decisions — adopted classes, and declines with reasons."""
    github = FakeGitHub({}, files={f"{bootstrap.REGISTRY}/README.md"})
    bootstrap.seed_registry(github)
    assert github.calls == []
