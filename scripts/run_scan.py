#!/usr/bin/env python3
"""Fire the nightly scan on demand.

The manual entry point is native: an enabled automation can be run immediately,
bypassing its trigger conditions. So this is one API call, not a feature.

    python -m scripts.run_scan
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.devin import DevinClient  # noqa: E402

NAME = os.environ.get("SCAN_AUTOMATION_NAME", "Superset — nightly scan")


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
    result = client.run_automation(match["automation_id"])
    print(f"fired {NAME} ({match['automation_id']}): {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
