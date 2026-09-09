#!/usr/bin/env python3
"""Fire the nightly scan on demand.

The automations API has no run-now endpoint, so the scan carries a second
trigger — ``webhook:incoming`` — whose URL the platform issues at creation.
Firing it is a POST to that URL; this script looks the URL up rather than
hardcoding it, so it survives the automation being recreated.

The trigger's secret is write-once: the API returns it as ``null`` and it is
readable only in the automation's UI page, so it is supplied out of band.

    SCAN_WEBHOOK_SECRET=... python -m scripts.run_scan
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.devin import DevinClient  # noqa: E402

NAME = os.environ.get("SCAN_AUTOMATION_NAME", "Superset — nightly scan")


def webhook_url(automation: dict[str, Any]) -> str | None:
    for trigger in automation.get("triggers", []):
        if trigger.get("event_type") == "webhook:incoming":
            return (trigger.get("webhook") or {}).get("url")
    return None


def main() -> int:
    api_key, org_id = os.environ.get("DEVIN_API_KEY", ""), os.environ.get("DEVIN_ORG_ID", "")
    if not api_key or not org_id:
        print("DEVIN_API_KEY and DEVIN_ORG_ID are required", file=sys.stderr)
        return 2

    client = DevinClient(api_key, org_id)
    match = next((a for a in client.list_automations() if a.get("name") == NAME), None)
    if not match:
        print(f"no automation named {NAME!r}; run apply_automations first", file=sys.stderr)
        return 1

    url = webhook_url(match)
    if not url:
        print(f"{NAME!r} has no webhook:incoming trigger; re-apply the definition", file=sys.stderr)
        return 1

    secret = os.environ.get("SCAN_WEBHOOK_SECRET", "")
    if not secret:
        print("SCAN_WEBHOOK_SECRET is required (copy it from the automation's page)", file=sys.stderr)
        return 2

    response = httpx.post(
        url,
        headers={"X-Webhook-Secret": secret},
        json={"reason": "manual run", "source": "scripts.run_scan"},
        timeout=60.0,
    )
    if response.status_code >= 400:
        print(f"POST {url} → {response.status_code}: {response.text[:300]}", file=sys.stderr)
        return 1
    print(f"fired {NAME} ({match['automation_id']}): {json.dumps(response.json())[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
