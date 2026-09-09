"""Devin API client.

Everything here is the v3 organization API, under service-user RBAC
(``ViewOrgAutomations`` / ``ManageOrgAutomations`` / ``ViewOrgSessions``).

``/v1/sessions`` is the personal surface and rejects a service key outright, so
it is not a fallback. That is the better surface anyway: the v3 session object
carries ``structured_output``, ``pull_requests`` and ``acus_consumed``, which is
every field the reconciler needs from one call.

With one exception. ``structured_output`` is only populated for a session
created with a schema attached, and the API rejects a schema on a session an
automation spawns — so for every session this system observes it is ``null``,
and the prompt asks for the same JSON in the final message instead. ``report``
reads it from whichever of the two is present.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


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
        """One call returns every session for our tag.

        Tagging every automation-spawned session means the reconciler makes a
        single request per cycle rather than one per task.
        """
        params: dict[str, Any] = {"limit": limit}
        if tags:
            params["tags"] = ",".join(tags)
        data = self._request("GET", self._org_path("sessions"), params=params)
        return data.get("items", []) if isinstance(data, dict) else []

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", self._org_path(f"sessions/{session_id}"))

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        data = self._request("GET", self._org_path(f"sessions/{session_id}/messages"))
        return data.get("items", []) if isinstance(data, dict) else []

    def report(self, session: dict[str, Any]) -> dict[str, Any]:
        """What the session said it did, from wherever it managed to say it.

        Costs one extra request per session, and only for sessions the platform
        left without ``structured_output`` — which is all of them today. Parse
        failures return ``{}`` rather than raising: a session that answered in
        prose is a session we know less about, not a broken cycle.
        """
        out = session.get("structured_output")
        if isinstance(out, dict) and out:
            return out
        session_id = session.get("session_id")
        if not session_id:
            return {}
        try:
            items = self.messages(str(session_id))
        except DevinError:
            return {}
        for item in reversed(items):
            text = item.get("message")
            if not isinstance(text, str):
                continue
            for block in reversed(JSON_BLOCK.findall(text)):
                try:
                    parsed = json.loads(block)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    # ------------------------------------------------------------ automations

    def _org_path(self, suffix: str = "") -> str:
        if not self.org_id:
            raise DevinError("DEVIN_ORG_ID is required for the organization API")
        return f"/v3/organizations/{self.org_id}/{suffix.lstrip('/')}"

    def list_automations(self) -> list[dict[str, Any]]:
        data = self._request("GET", self._org_path("automations"))
        return data.get("items", []) if isinstance(data, dict) else []

    def automation_schemas(self) -> dict[str, Any]:
        """The platform's trigger catalogue: event types, their filterable fields,
        and which reply kinds each supports.

        There is no server-side dry-run endpoint, so this is what ``--check``
        validates against — the authoritative shape, fetched rather than assumed.
        """
        data = self._request("GET", self._org_path("automations/schemas"))
        return data if isinstance(data, dict) else {}

    def create_automation(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._org_path("automations"), json=spec)

    def update_automation(self, automation_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", self._org_path(f"automations/{automation_id}"), json=spec)

    def close(self) -> None:
        self._client.close()
