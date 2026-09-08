"""GitHub client — detection of queued work, and cleanup of rejected work.

Deliberately small. GitHub is *not* consulted for PR state: the Devin session
object already reports it, and asking both would introduce a third opinion for
no gain.

There is no merge call in this file. That is the point, and it is enforced by a
test.
"""

from __future__ import annotations

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

    def issues_with_label(self, label: str, state: str = "all") -> list[dict[str, Any]]:
        return self._request(
            "GET",
            f"/repos/{self.repo}/issues",
            params={"labels": label, "state": state, "per_page": 100},
        )

    def add_label(self, issue_number: int, label: str) -> Any:
        """Fallback path only.

        Used if the platform filters events its own bots caused, in which case
        the scanner's label never reaches the remediator and we apply it from
        here instead.
        """
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
