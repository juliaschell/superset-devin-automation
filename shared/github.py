"""GitHub client: find queued work, clean up rejected work.

Small on purpose. PR state is read from the Devin session object, which already
reports it, rather than from here as well.

There is no merge call in this file, and a test keeps it that way.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, repo: str, base_url: str = "https://api.github.com") -> None:
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        if response.status_code >= 400:
            raise GitHubError(f"{method} {path} → {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    def repository(self) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}")

    def enable_issues(self) -> Any:
        """Forks have Issues off by default, and the pipeline files issues."""
        return self._request("PATCH", f"/repos/{self.repo}", json={"has_issues": True})

    def viewer(self) -> str:
        """The account the token belongs to."""
        return str(self._request("GET", "/user")["login"])

    def fork(self, upstream: str) -> Any:
        """Fork into exactly `self.repo`.

        Without `name` the fork keeps the upstream's name, and without
        `organization` it lands under the token's own account — so asking for
        `you/anything-else` silently produces `you/superset`, and the wait for
        the requested name never ends.

        The reply names the repo that was actually forked, which is not always
        the one asked for: an account holds one fork of an upstream whatever it
        is called, and a second request returns the first.
        """
        owner, _, name = self.repo.partition("/")
        payload: dict[str, Any] = {"name": name}
        if owner != self.viewer():
            payload["organization"] = owner
        return self._request("POST", f"/repos/{upstream}/forks", json=payload)

    def ensure_label(self, name: str, color: str, description: str) -> bool:
        """Create the label if it is missing. Returns True if it was created."""
        try:
            self._request("GET", f"/repos/{self.repo}/labels/{name}")
        except GitHubError:
            self._request(
                "POST",
                f"/repos/{self.repo}/labels",
                json={"name": name, "color": color, "description": description},
            )
            return True
        return False

    def file_exists(self, path: str) -> bool:
        try:
            self._request("GET", f"/repos/{self.repo}/contents/{path}")
        except GitHubError:
            return False
        return True

    def put_file(self, path: str, content: str, message: str) -> Any:
        """Commit a file to the default branch; the API refuses to overwrite
        without a blob sha, and there is deliberately no update path. This
        seeds the registry into a fresh fork; humans edit it after that.
        """
        return self._request(
            "PUT",
            f"/repos/{self.repo}/contents/{path}",
            json={
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
            },
        )

    def issues_with_label(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{self.repo}/issues",
            params={"labels": label, "state": state, "per_page": 100},
        )

    def create_issue(self, title: str, body: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/repos/{self.repo}/issues", json={"title": title, "body": body}
        )

    def add_label(self, issue_number: int, label: str) -> Any:
        return self._request(
            "POST", f"/repos/{self.repo}/issues/{issue_number}/labels", json={"labels": [label]}
        )

    def comment(self, issue_number: int, body: str) -> Any:
        return self._request(
            "POST", f"/repos/{self.repo}/issues/{issue_number}/comments", json={"body": body}
        )

    def close_issue(self, issue_number: int) -> Any:
        return self._request(
            "PATCH", f"/repos/{self.repo}/issues/{issue_number}", json={"state": "closed"}
        )

    def close_pull_request(self, number: int) -> Any:
        return self._request("PATCH", f"/repos/{self.repo}/pulls/{number}", json={"state": "closed"})

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/repos/{self.repo}/pulls/{number}")

    def delete_branch(self, branch: str) -> Any:
        return self._request("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}")

    def close(self) -> None:
        self._client.close()


def pr_number_from_url(url: str) -> int | None:
    """``https://github.com/o/r/pull/42`` → ``42``."""
    parts = [p for p in url.rstrip("/").split("/") if p]
    if len(parts) >= 2 and parts[-2] == "pull" and parts[-1].isdigit():
        return int(parts[-1])
    return None


def repo_from_pr_url(url: str) -> str | None:
    """``https://github.com/o/r/pull/42`` → ``o/r``.

    A number alone is ambiguous across forks: PR 11 exists on every one of
    them, and asking the wrong fork about it answers about someone else's work.
    """
    parts = [p for p in url.rstrip("/").split("/") if p]
    if len(parts) >= 4 and parts[-2] == "pull":
        return f"{parts[-4]}/{parts[-3]}"
    return None
