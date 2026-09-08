"""State: one SQLite file, two tables, stdlib ``sqlite3``.

The two tables are here for different reasons, and conflating them is how this
design goes wrong:

``task_events`` is append-only and is justified because **the data exists
nowhere else** — state transitions, our own decisions, and the timing history
every metric derives from. It is history, so it cannot disagree with anything.

``tasks`` is a materialized view. Every field in it is re-derivable from Devin
and GitHub, so it is *not* justified on those grounds. It earns its place on
three narrower ones: read cost (otherwise every dashboard render fans out to two
APIs), dedup via a uniqueness constraint, and our own annotations — rejection,
cleanup state, attempt chains — which Devin has no concept of.

Delete this file and you lose history, not correctness.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# The funnel, in order. "Furthest reached" is computed against this sequence,
# because current-state counts render a finished run as a row of zeroes with
# everything piled in the last column.
STAGES = ["detected", "dispatched", "running", "pr_open", "verified", "merged"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    issue_number        INTEGER PRIMARY KEY,
    title               TEXT    NOT NULL DEFAULT '',
    issue_url           TEXT    NOT NULL DEFAULT '',
    classification      TEXT,
    stage               TEXT    NOT NULL DEFAULT 'detected',
    session_id          TEXT,
    session_status      TEXT,
    pr_url              TEXT,
    pr_state            TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    validate_command    TEXT,
    validate_registry   TEXT,
    validation_passed   INTEGER,
    outcome             TEXT,
    acus                REAL,
    failure_reason      TEXT,
    human_messages      INTEGER NOT NULL DEFAULT 0,
    rejected            INTEGER NOT NULL DEFAULT 0,
    cleaned_up          INTEGER NOT NULL DEFAULT 0,
    detected_at         REAL,
    dispatched_at       REAL,
    pr_opened_at        REAL,
    settled_at          REAL,
    updated_at          REAL
);

CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number    INTEGER,
    session_id      TEXT,
    kind            TEXT NOT NULL,
    detail          TEXT,
    payload         TEXT,
    ts              REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON task_events(issue_number);
CREATE INDEX IF NOT EXISTS idx_events_kind  ON task_events(kind);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL lets the dashboard read while the reconciler writes; busy_timeout
        # covers the moment they collide.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        """``CREATE TABLE IF NOT EXISTS`` leaves an older file on its old shape,
        so new columns are added here rather than by rebuilding the database."""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, decl in (("acus", "REAL"),):
            if name not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")

    # ---------------------------------------------------------------- events

    def log(
        self,
        kind: str,
        issue_number: int | None = None,
        session_id: str | None = None,
        detail: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO task_events (issue_number, session_id, kind, detail, payload, ts)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                issue_number,
                session_id,
                kind,
                detail,
                json.dumps(payload) if payload else None,
                time.time(),
            ),
        )
        self.conn.commit()

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM task_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- tasks

    def upsert_task(self, issue_number: int, **fields: Any) -> bool:
        """Insert or update a task. Returns True if this created a new row.

        Devin and GitHub are authoritative for everything they report, so this
        overwrites rather than merging. ``None`` values are ignored so a partial
        observation never blanks a field we already know.
        """
        fields = {k: v for k, v in fields.items() if v is not None}
        now = time.time()
        existing = self.get_task(issue_number)
        if existing is None:
            cols = ["issue_number", "detected_at", "updated_at", *fields.keys()]
            vals = [issue_number, now, now, *fields.values()]
            placeholders = ", ".join("?" * len(cols))
            self.conn.execute(
                f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({placeholders})", vals
            )
            self.conn.commit()
            return True
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE tasks SET {assignments}, updated_at = ? WHERE issue_number = ?",
            [*fields.values(), now, issue_number],
        )
        self.conn.commit()
        return False

    def get_task(self, issue_number: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE issue_number = ?", (issue_number,)
        ).fetchone()
        return dict(row) if row else None

    def tasks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tasks ORDER BY COALESCE(updated_at, 0) DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def advance(self, issue_number: int, stage: str, detail: str | None = None) -> None:
        """Record that a task reached ``stage``.

        Monotonic: an observation implying an earlier stage (a session object
        that has not caught up, say) never rewinds a task that already opened a
        PR.
        """
        task = self.get_task(issue_number)
        if task and task["stage"] == stage:
            return
        if task and stage in STAGES and task["stage"] in STAGES:
            if STAGES.index(stage) < STAGES.index(task["stage"]):
                return
        stamp: dict[str, Any] = {"stage": stage}
        if stage == "dispatched":
            stamp["dispatched_at"] = time.time()
        elif stage == "pr_open":
            stamp["pr_opened_at"] = time.time()
        elif stage in ("verified", "merged"):
            stamp["settled_at"] = time.time()
        self.upsert_task(issue_number, **stamp)
        self.log(
            "stage",
            issue_number=issue_number,
            detail=stage,
            payload={"note": detail} if detail else None,
        )

    def furthest_stages(self) -> dict[str, int]:
        """Cumulative funnel: how many tasks *ever reached* each stage.

        Derived from the event log rather than current state — the whole reason
        the log exists.
        """
        counts = dict.fromkeys(STAGES, 0)
        # Every known task reached "detected" by definition, whether or not a
        # transition was ever logged for it.
        reached: dict[int, set[str]] = {
            row["issue_number"]: {"detected"}
            for row in self.conn.execute("SELECT issue_number FROM tasks").fetchall()
        }
        rows = self.conn.execute(
            "SELECT issue_number, detail FROM task_events WHERE kind = 'stage'"
        ).fetchall()
        for row in rows:
            if row["issue_number"] is None:
                continue
            reached.setdefault(row["issue_number"], {"detected"}).add(row["detail"])
        for stages in reached.values():
            depth = max((STAGES.index(s) for s in stages if s in STAGES), default=0)
            for stage in STAGES[: depth + 1]:
                counts[stage] += 1
        return counts

    # ------------------------------------------------------------------ meta

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def bulk_log(self, events: Iterable[dict[str, Any]]) -> None:
        for event in events:
            self.log(**event)
