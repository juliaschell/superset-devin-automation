#!/usr/bin/env python3
"""Apply the checked-in playbooks to Devin — infrastructure as code, as with the
automations.

A playbook is the procedure a remediation session follows, held by the platform
rather than pasted into an automation prompt: the prompt binds it to a repo and
a trigger, the playbook says how the work is done. It also carries the output
schema, which an automation's ``start_session`` action has no field for.

    python -m bootstrap.playbooks --check   # render only, writes nothing
    python -m bootstrap.playbooks           # create or update
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.devin import DevinClient, DevinError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# One per system that has a procedure worth holding on the platform.
PLAYBOOKS = [ROOT / "remediator" / "playbook.md"]


def parse(path: Path) -> dict[str, Any]:
    """Split a playbook file into its frontmatter and its body.

    The frontmatter is three keys and nothing nests, so it is read line by line
    rather than adding a YAML dependency for it.
    """
    text = path.read_text()
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name}: expected a --- frontmatter block")
    front, body = text[4:].split("\n---\n", 1)
    meta: dict[str, str] = {}
    for line in front.splitlines():
        if not line.strip():
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')

    spec: dict[str, Any] = {"title": meta["title"], "body": body.strip()}
    if macro := meta.get("macro"):
        spec["macro"] = macro
    if schema := meta.get("schema"):
        spec["structured_output_schema"] = json.loads((path.parent / schema).read_text())
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="render only; write nothing")
    args = parser.parse_args(argv)

    specs = [parse(path) for path in PLAYBOOKS]
    for spec in specs:
        print(f"✓ {spec['title']}: {len(spec['body'])} chars, macro {spec.get('macro', '—')}")
    if args.check:
        return 0

    api_key, org_id = os.environ.get("DEVIN_API_KEY", ""), os.environ.get("DEVIN_ORG_ID", "")
    if not api_key or not org_id:
        print("DEVIN_API_KEY and DEVIN_ORG_ID are required", file=sys.stderr)
        return 2

    client = DevinClient(api_key, org_id)
    try:
        existing = {p.get("title"): p for p in client.list_playbooks()}
        for spec in specs:
            current = existing.get(spec["title"])
            if current:
                client.update_playbook(current["playbook_id"], spec)
                print(f"↻ {spec['title']}: updated ({current['playbook_id']})")
            else:
                created = client.create_playbook(spec)
                print(f"+ {spec['title']}: created ({created.get('playbook_id')})")
    except DevinError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
