"""The fork call, which has to name its destination.

Everything else in the client is a thin request; this one has a default that
silently does the wrong thing.
"""

from __future__ import annotations

import httpx

from shared.github import GitHubClient


def client_recording(requests: list[httpx.Request], viewer: str) -> GitHubClient:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": viewer})
        return httpx.Response(202, json={})

    github = GitHubClient("token", "acme/other-name")
    github._client = httpx.Client(transport=httpx.MockTransport(handler))
    return github


def test_a_fork_asks_for_the_repo_name_it_was_given() -> None:
    """Without `name`, GitHub keeps the upstream's, and the wait never ends."""
    requests: list[httpx.Request] = []
    client_recording(requests, viewer="acme").fork("apache/superset")
    assert requests[-1].url.path == "/repos/apache/superset/forks"
    assert requests[-1].read() == b'{"name":"other-name"}'


def test_a_fork_into_someone_elses_account_names_the_organization() -> None:
    requests: list[httpx.Request] = []
    client_recording(requests, viewer="me").fork("apache/superset")
    assert requests[-1].read() == b'{"name":"other-name","organization":"acme"}'
