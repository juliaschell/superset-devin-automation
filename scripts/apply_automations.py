#!/usr/bin/env python3
"""Apply the checked-in automation definitions to Devin — infrastructure as code.

The definitions in ``automations/*.json`` are the source of truth; this script
makes the org match them. Prompts live in ``automations/prompts/*.md`` and
output schemas in ``automations/schemas/*.json``, and are substituted in, so the
prose is reviewable as prose in a diff.

    python -m scripts.apply_automations --check   # validate only, creates nothing
    python -m scripts.apply_automations           # create or update
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.devin import DevinClient, DevinError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
AUTOMATIONS = ROOT / "automations"

DEFINITIONS = {
    "scan": {"spec": "scan.json", "prompt": "scan.md", "schema": "scan_output.json"},
    "remediate": {"spec": "remediate.json", "prompt": "remediate.md", "schema": "remediate_output.json"},
}


def render(role: str, repo: str, max_issues: int) -> dict[str, Any]:
    files = DEFINITIONS[role]
    spec = json.loads((AUTOMATIONS / files["spec"]).read_text())
    prompt = (AUTOMATIONS / "prompts" / files["prompt"]).read_text()
    schema = json.loads((AUTOMATIONS / "schemas" / files["schema"]).read_text())

    prompt = prompt.replace("{{REPO}}", repo).replace("{{MAX_ISSUES_PER_RUN}}", str(max_issues))
    # The schema is appended to the prompt as well as attached to the action:
    # attaching it makes the platform enforce the shape, restating it makes the
    # session aware of the field names it is being held to.
    prompt += (
        "\n\n## Output schema\n\nReturn structured output matching exactly:\n\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
    )

    body = json.dumps(spec)
    body = body.replace("{{REPO}}", repo)
    spec = json.loads(body)
    for action in spec.get("actions", []):
        if action.get("type") == "start_session":
            action["prompt"] = prompt
            action.setdefault("session", {})["structured_output_schema"] = schema
    return spec


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate only; create nothing")
    parser.add_argument("--repo", default=os.environ.get("REPO", "juliaschell/superset"))
    parser.add_argument("--max-issues", type=int, default=int(os.environ.get("MAX_ISSUES_PER_RUN", "8")))
    parser.add_argument("--only", choices=sorted(DEFINITIONS), help="apply a single automation")
    args = parser.parse_args()

    api_key, org_id = os.environ.get("DEVIN_API_KEY", ""), os.environ.get("DEVIN_ORG_ID", "")
    if not api_key or not org_id:
        print("DEVIN_API_KEY and DEVIN_ORG_ID are required", file=sys.stderr)
        return 2

    client = DevinClient(api_key, org_id)
    existing = {a.get("name"): a for a in client.list_automations()}
    roles = [args.only] if args.only else list(DEFINITIONS)
    failed = False

    for role in roles:
        spec = render(role, args.repo, args.max_issues)
        name = spec["name"]
        try:
            client.validate_automation({"action": "validate_create", **spec})
            print(f"✓ {name}: payload valid")
        except DevinError as exc:
            print(f"✗ {name}: {exc}", file=sys.stderr)
            failed = True
            continue
        if args.check:
            continue
        current = existing.get(name)
        if current:
            client.update_automation(current["automation_id"], spec)
            print(f"↻ {name}: updated ({current['automation_id']})")
        else:
            created = client.create_automation(spec)
            print(f"+ {name}: created ({created.get('automation_id')})")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
