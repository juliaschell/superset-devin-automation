"""Devin API client, v3 only.

``/v1/sessions`` is the personal surface and rejects a service key, so it is
not a fallback. v3 is the better surface anyway: one call per cycle returns
every tagged session with its pull requests attached.

``report`` and ``session_acus`` work around two fields that do not behave as
documented — each says how.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# A session's first message is the trigger payload, itself fenced JSON, so
# "newest JSON block" alone would read a GitHub webhook as the report. These
# keys are what mark out the two output schemas.
REPORT_KEYS = frozenset({"outcome", "issues_filed", "classes_proposed"})


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
        # Retried once, because the read timeouts seen here are transient. A
        # second failure is a DevinError, which callers treat as "we know less
        # this cycle" rather than as a reason to abort the pass.
        for attempt in (1, 2):
            try:
                response = self._client.request(method, f"{self.base_url}{path}", **kwargs)
                break
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise DevinError(f"{method} {path} → {exc!r}") from exc
        if response.status_code >= 400:
            raise DevinError(f"{method} {path} → {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    # -------------------------------------------------------------- sessions

    def list_sessions(self, tags: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Every session carrying our tag: one request per cycle rather than
        one per task, which is why the automations tag what they spawn."""
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

        ``structured_output`` is populated only when a schema was attached at
        creation, and the API rejects a schema on an automation-spawned
        session. So it is always null here, and the prompt asks for the same
        JSON in the final message instead.

        ``{}`` rather than an error when nothing parses: a session that
        answered in prose, or has not answered yet, is one we know less about.
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
                if isinstance(parsed, dict) and REPORT_KEYS & parsed.keys():
                    return parsed
        return {}

    def session_acus(self, session_id: str) -> float | None:
        """What the session cost, from the billing surface.

        ``acus_consumed`` on the session object reads ``0.0`` even for sessions
        that plainly did work, so the figure comes from the consumption API
        instead — the data behind the usage dashboard.

        ``None`` rather than ``0.0`` when there are no rows: unknown is not
        free. Below Enterprise the endpoint returns an empty series, so
        ``None`` is the usual answer on a self-serve account.
        """
        ident = session_id if session_id.startswith("devin-") else f"devin-{session_id}"
        try:
            data = self._request("GET", self._org_path(f"consumption/daily/sessions/{ident}"))
        except DevinError:
            return None
        if not isinstance(data, dict) or not data.get("consumption_by_date"):
            return None
        total = data.get("total_acus")
        return float(total) if isinstance(total, int | float) else None

    # ------------------------------------------------------------ automations

    def _org_path(self, suffix: str = "") -> str:
        if not self.org_id:
            raise DevinError("DEVIN_ORG_ID is required for the organization API")
        return f"/v3/organizations/{self.org_id}/{suffix.lstrip('/')}"

    def list_automations(self) -> list[dict[str, Any]]:
        data = self._request("GET", self._org_path("automations"))
        return data.get("items", []) if isinstance(data, dict) else []

    def automation_schemas(self) -> dict[str, Any]:
        """The platform's trigger catalogue: event types, filterable fields and
        supported actions. There is no dry-run endpoint, so ``--check``
        validates a definition against this."""
        data = self._request("GET", self._org_path("automations/schemas"))
        return data if isinstance(data, dict) else {}

    def create_automation(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._org_path("automations"), json=spec)

    def update_automation(self, automation_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", self._org_path(f"automations/{automation_id}"), json=spec)

    # -------------------------------------------------------------- playbooks

    def list_playbooks(self) -> list[dict[str, Any]]:
        data = self._request("GET", self._org_path("playbooks"))
        return data.get("items", []) if isinstance(data, dict) else []

    def create_playbook(self, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self._org_path("playbooks"), json=spec)

    def update_playbook(self, playbook_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", self._org_path(f"playbooks/{playbook_id}"), json=spec)

    def close(self) -> None:
        self._client.close()
