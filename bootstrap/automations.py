#!/usr/bin/env python3
"""Apply the checked-in automation definitions to Devin — automations as code.

``scanner/`` and ``remediator/`` each hold an ``automation.json``, the
``prompt.md`` it runs and the ``output_schema.json`` it is asked to return.
Those files are the source of truth; this makes the org match them, keeping the
prompts reviewable as prose rather than as JSON strings.

    python -m bootstrap.automations --check   # validate only, creates nothing
    python -m bootstrap.automations           # create or update
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from shared.devin import DevinClient, DevinError

ROOT = Path(__file__).resolve().parent.parent

# Directory per system, same three filenames in each.
DEFINITIONS: dict[str, dict[str, str]] = {
    "scanner": {},
    "remediator": {"playbook": "Superset remediation"},
}


def strip_docs(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_docs(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [strip_docs(v) for v in value]
    return value


def render(role: str, repo: str, max_issues: int, playbook_ids: dict[str, str]) -> dict[str, Any]:
    files, home = DEFINITIONS[role], ROOT / role
    spec = json.loads((home / "automation.json").read_text())
    prompt = (home / "prompt.md").read_text()

    prompt = prompt.replace("{{REPO}}", repo).replace("{{MAX_ISSUES_PER_RUN}}", str(max_issues))
    if title := files.get("playbook"):
        # A session's playbook_id is read-only and derived from this token.
        # Looked up by title so no platform id is checked in.
        prompt = f"@playbook:{playbook_ids[title]}\n\n{prompt}"
    if (schema_path := home / "output_schema.json").exists():
        # In the prompt because nothing else carries a schema to a spawned
        # session: the automations API has no field for one, and a playbook's
        # `structured_output_schema` was measured not to reach the session.
        # So the shape is requested, not enforced, and the watcher treats every
        # field of it as optional.
        schema = json.loads(schema_path.read_text())
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
    """Validate a spec against the platform's trigger catalogue.

    There is no dry-run endpoint, and these are the mistakes that fail silently:
    a trigger naming an event type or condition field the platform does not
    publish is accepted, never fires, and reports nothing.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="validate only; create nothing")
    parser.add_argument("--repo", default=os.environ.get("REPO", ""))
    parser.add_argument("--max-issues", type=int, default=int(os.environ.get("MAX_ISSUES_PER_RUN", "8")))
    parser.add_argument("--only", choices=sorted(DEFINITIONS), help="apply a single automation")
    args = parser.parse_args(argv)

    if not args.repo:
        print("REPO (or --repo) is required, as owner/name", file=sys.stderr)
        return 2

    api_key, org_id = os.environ.get("DEVIN_API_KEY", ""), os.environ.get("DEVIN_ORG_ID", "")
    if not api_key or not org_id:
        print("DEVIN_API_KEY and DEVIN_ORG_ID are required", file=sys.stderr)
        return 2

    client = DevinClient(api_key, org_id)
    try:
        schemas = client.automation_schemas()
        existing = {a.get("name"): a for a in client.list_automations()}
        playbook_ids = {p["title"]: p["playbook_id"] for p in client.list_playbooks()}
    except DevinError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    roles = [args.only] if args.only else list(DEFINITIONS)
    failed = False

    for role in roles:
        needed = DEFINITIONS[role].get("playbook")
        if needed and needed not in playbook_ids:
            print(
                f"✗ {role}: playbook {needed!r} does not exist; run apply_playbooks first",
                file=sys.stderr,
            )
            failed = True
            continue
        spec = render(role, args.repo, args.max_issues, playbook_ids)
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
