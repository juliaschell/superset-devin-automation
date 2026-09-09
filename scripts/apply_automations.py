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


def strip_docs(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_docs(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_docs(v) for v in value]
    return value


def render(role: str, repo: str, max_issues: int) -> dict[str, Any]:
    files = DEFINITIONS[role]
    spec = json.loads((AUTOMATIONS / files["spec"]).read_text())
    prompt = (AUTOMATIONS / "prompts" / files["prompt"]).read_text()
    schema = json.loads((AUTOMATIONS / "schemas" / files["schema"]).read_text())

    prompt = prompt.replace("{{REPO}}", repo).replace("{{MAX_ISSUES_PER_RUN}}", str(max_issues))
    # The schema goes in the prompt because the automations API has no field to
    # attach one to a spawned session. The shape is therefore requested, not
    # enforced — so the reconciler treats every structured-output field as
    # optional and records a parse failure rather than assuming it is present.
    prompt += (
        "\n\n## Output schema\n\nReturn structured output matching exactly:\n\n```json\n"
        + json.dumps(schema, indent=2)
        + "\n```\n"
    )

    body = json.dumps(spec)
    body = body.replace("{{REPO}}", repo)
    # JSON has no comments and the API rejects unknown keys, so rationale lives
    # under _-prefixed keys that are stripped on the way out.
    spec = strip_docs(json.loads(body))
    for action in spec.get("actions", []):
        if action.get("type") == "start_session":
            action["prompt"] = prompt
    return spec


def check_against_schemas(spec: dict[str, Any], schemas: dict[str, Any]) -> list[str]:
    """Validate a spec against the platform's own trigger catalogue.

    There is no server-side dry-run endpoint, so the next best thing is to check
    the parts that fail silently rather than loudly: a trigger that names an
    event type or a condition field the platform does not publish never fires,
    and produces no error anywhere.
    """
    catalogue: dict[str, dict[str, Any]] = {}
    for source in schemas.get("sources", {}).values():
        for event in source.values():
            catalogue[event["event_type"]] = event

    problems = []
    for trigger in spec.get("triggers", []):
        event_type = trigger.get("event_type", "")
        event = catalogue.get(event_type)
        if event is None:
            problems.append(f"unknown event_type {event_type!r}")
            continue
        fields = event.get("fields", {})
        for group in trigger.get("conditions", {}).get("any", []):
            for condition in group.get("all", []):
                field = condition.get("field", "")
                if field not in fields:
                    problems.append(f"{event_type}: no filterable field {field!r}")
                options = (fields.get(field) or {}).get("options")
                if options and condition.get("value") not in {o["value"] for o in options}:
                    problems.append(f"{event_type}: {field}={condition.get('value')!r} is not an allowed value")
        supported = set(event.get("supported_replies") or [])
        for reply in trigger.get("replies", []):
            if reply.get("type") not in supported:
                problems.append(f"{event_type} does not support reply {reply.get('type')!r}")

    if not spec.get("session_settings", {}).get("net_policy", {}).get("allow"):
        # Omitting the allowlist blocks all egress, which looks like a hung session.
        problems.append("session_settings.net_policy.allow is empty; egress would be blocked")
    return problems


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
    try:
        schemas = client.automation_schemas()
        existing = {a.get("name"): a for a in client.list_automations()}
    except DevinError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    roles = [args.only] if args.only else list(DEFINITIONS)
    failed = False

    for role in roles:
        spec = render(role, args.repo, args.max_issues)
        name = spec["name"]
        problems = check_against_schemas(spec, schemas)
        if problems:
            for problem in problems:
                print(f"✗ {name}: {problem}", file=sys.stderr)
            failed = True
            continue
        print(f"✓ {name}: triggers match the platform schema")
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
