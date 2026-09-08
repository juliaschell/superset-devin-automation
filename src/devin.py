"""Devin API client.

Two API surfaces are in play and they are not interchangeable:

- ``/v1/sessions``  — session state. Any key can read it.
- ``/v3/organizations/{org}/automations`` — automation CRUD and *run now*.
  Service-user RBAC only (``ViewOrgAutomations`` / ``ManageOrgAutomations``);
  a personal key is rejected here.
"""

from __future__ import annotations

from typing import Any

import httpx


class DevinError(RuntimeError):
    pass


class DevinClient:
    def __init__(self, api_key: str, org_id: str = "", base_url: str = "https://api.devin.ai") -> None:
        self.base_url = base_url.rstrip("/")
        self.org_id = org_id
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
        if response.status_code >= 400:
            raise DevinError(f"{method} {path} → {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    # -------------------------------------------------------------- sessions

    def list_sessions(self, tags: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """One call returns every in-flight session for our tag.

        Tagging every automation-spawned session means the reconciler makes a
        single request per cycle rather than one per task.
        """
        params: dict[str, Any] = {"limit": limit}
        if tags:
            params["tags"] = ",".join(tags)
        data = self._request("GET", "/v1/sessions", params=params)
        return data.get("sessions", []) if isinstance(data, dict) else []

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/session/{session_id}")

    # ------------------------------------------------------------ automations

    def _org_path(self, suffix: str = "") -> str:
        if not self.org_id:
            raise DevinError("DEVIN_ORG_ID is required for automation endpoints")
        return f"/v3/organizations/{self.org_id}/automations{suffix}"

    def list_automations(self) -> list[dict[str, Any]]:
        data = self._request("GET", self._org_path())
        return data.get("items", []) if isinstance(data, dict) else []

    def validate_automation(self, spec: dict[str, Any]) -> Any:
        """Dry run. Creates nothing, so it is safe to run in CI."""
        return self._request("POST", self._org_path("/validate"), json=spec)

    def create_automation(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._org_path(), json=spec)

    def update_automation(self, automation_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", self._org_path(f"/{automation_id}"), json=spec)

    def run_automation(self, automation_id: str) -> Any:
        """Fire an enabled automation immediately, bypassing its triggers.

        This is the manual entry point for the nightly scan — no second trigger
        and no button of our own.
        """
        return self._request("POST", self._org_path(f"/{automation_id}/run"))

    def close(self) -> None:
        self._client.close()
