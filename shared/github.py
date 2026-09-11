"""GitHub client: find queued work, clean up rejected work.

Small on purpose — PR state comes from the Devin session object, which already
reports it. There is no merge call here, and a test keeps it that way.
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

        Without `name` the fork keeps the upstream's, and without
        `organization` it lands under the token's own account: asking for
        `you/anything-else` then silently produces `you/superset` and the wait
        never ends.

        The reply names the repo actually forked, which is not always the one
        asked for — an account holds one fork of an upstream whatever it is
        called, and a second request returns the first.
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
        """Commit a file to the default branch. Deliberately no update path:
        this seeds the registry into a fresh fork, and humans own it after.
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

    def open_pull_requests(self) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100}
        )

    def changes_requested(self, number: int) -> tuple[int, str | None]:
        """How many times a human sent this PR back, and when the last one
        was. Nothing acts on it; the timestamp says whether an answer has
        landed since."""
        reviews = self._request(
            "GET", f"/repos/{self.repo}/pulls/{number}/reviews", params={"per_page": 100}
        )
        sent_back = [r for r in reviews or [] if r.get("state") == "CHANGES_REQUESTED"]
        last = sent_back[-1].get("submitted_at") if sent_back else None
        return len(sent_back), (str(last) if last else None)

    def last_devin_activity_at(self, number: int) -> str | None:
        """When Devin last answered on this PR. A reply explaining why the
        review needs no change counts as much as a commit does."""
        commits = self._request(
            "GET", f"/repos/{self.repo}/pulls/{number}/commits", params={"per_page": 100}
        )
        comments = self._request(
            "GET", f"/repos/{self.repo}/issues/{number}/comments", params={"per_page": 100}
        )
        stamps = [
            (((commits or [{}])[-1].get("commit") or {}).get("committer") or {}).get("date"),
            *(
                c.get("created_at")
                for c in comments or []
                if str((c.get("user") or {}).get("login", "")).startswith("devin-ai-integration")
            ),
        ]
        seen = [str(s) for s in stamps if s]
        return max(seen) if seen else None

    def pull_request_paths(self, number: int) -> list[str]:
        files = self._request(
            "GET", f"/repos/{self.repo}/pulls/{number}/files", params={"per_page": 100}
        )
        return [str(f.get("filename", "")) for f in files or []]

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

    A number alone is ambiguous: PR 11 exists on every fork, and asking the
    wrong one about it answers about someone else's work.
    """
    parts = [p for p in url.rstrip("/").split("/") if p]
    if len(parts) >= 4 and parts[-2] == "pull":
        return f"{parts[-4]}/{parts[-3]}"
    return None
