"""One SQLite file, two tables, stdlib ``sqlite3``.

``task_events`` is the append-only history: transitions, our own decisions, and
the timings every metric is derived from. It exists nowhere else.

``tasks`` is a cache of the latest state, all of it re-derivable from Devin and
GitHub. It saves the dashboard two API fan-outs per render, and holds the few
fields those APIs have no concept of: rejection, cleanup, attempt chains.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

# The funnel, in order. Counts are "ever reached" rather than current state,
# which would render a finished run as zeroes with everything in the last column.
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
        # WAL lets the dashboard read while the watcher writes; busy_timeout
        # covers the moment they collide.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` leaves an existing file on its old
        shape, so a column added later is added here rather than by rebuilding
        the database."""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, decl in (("acus", "REAL"),):
            if name not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")

    def bind_repo(self, repo: str) -> bool:
        """Point this database at ``repo``, emptying it if it held another.

        The database outlives the container it runs in, so pointing the same
        volume at a fresh fork would otherwise measure a funnel of issue
        numbers that no longer exist. Returns True if anything was discarded.
        """
        previous = self.get_meta("repo")
        changed = previous is not None and previous != repo
        if changed:
            self.conn.execute("DELETE FROM tasks")
            self.conn.execute("DELETE FROM task_events")
            self.conn.execute("DELETE FROM meta WHERE key <> 'repo'")
            self.conn.commit()
        self.set_meta("repo", repo)
        if changed:
            self.log("repo_changed", detail=f"{previous} → {repo}: cleared previous state")
        return changed

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
        # Also to the terminal `make up` is already holding: an event is a
        # state change, so this narrates the run without anyone opening the
        # dashboard, and stays quiet on the cycles where nothing happened.
        where = f" #{issue_number}" if issue_number else ""
        print(f"{time.strftime('%H:%M:%S')} {kind}{where} {detail or ''}".rstrip(), flush=True)

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM task_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- tasks

    def upsert_task(self, issue_number: int, **fields: Any) -> bool:
        """Insert or update a task. Returns True if this created a new row.

        Devin and GitHub are authoritative, so this overwrites rather than
        merges. ``None`` is ignored, so a partial observation never blanks a
        field already known.
        """
        fields = {k: v for k, v in fields.items() if v is not None}
        now = time.time()
        existing = self.get_task(issue_number)
        if existing is None:
            # Only fall back to this clock when the caller does not know
            # GitHub's timestamp for the issue.
            defaults = {
                k: now for k in ("detected_at", "updated_at") if k not in fields
            }
            cols = ["issue_number", *defaults.keys(), *fields.keys()]
            vals = [issue_number, *defaults.values(), *fields.values()]
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

    def session_ids_for(self, issue_number: int) -> set[str]:
        """Every session ever observed against this issue.

        Attempts are counted from this set, because the API lists an issue's
        sessions in no fixed order: comparing against the session on the row
        would alternate and count a fresh attempt on every poll.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT session_id FROM task_events"
            " WHERE issue_number = ? AND session_id IS NOT NULL",
            (issue_number,),
        ).fetchall()
        return {row["session_id"] for row in rows}

    def advance(self, issue_number: int, stage: str, detail: str | None = None) -> None:
        """Record that a task reached ``stage``. Monotonic: a stale observation
        never rewinds a task that already opened a PR."""
        task = self.get_task(issue_number)
        if task and task["stage"] == stage:
            return
        if task and stage in STAGES and task["stage"] in STAGES:
            if STAGES.index(stage) < STAGES.index(task["stage"]):
                return
        stamp: dict[str, Any] = {"stage": stage}
        # Never restamped, and only used where GitHub gave us no timestamp of
        # its own: this clock knows when we looked, not when the work happened.
        column = {
            "dispatched": "dispatched_at",
            "pr_open": "pr_opened_at",
            "verified": "settled_at",
            "merged": "settled_at",
        }.get(stage)
        if column and not (task or {}).get(column):
            stamp[column] = time.time()
        self.upsert_task(issue_number, **stamp)
        self.log(
            "stage",
            issue_number=issue_number,
            detail=stage,
            payload={"note": detail} if detail else None,
        )

    def furthest_stages(self) -> dict[str, int]:
        """Cumulative funnel: how many tasks ever reached each stage, from the
        event log rather than from current state."""
        counts = dict.fromkeys(STAGES, 0)
        # Every known task reached "detected", logged transition or not.
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

    def set_sessions(self, sessions: list[dict[str, Any]]) -> None:
        """What Devin is doing right now, as last seen.

        A scan belongs to no issue and a remediation does not name its issue
        until it reports, so neither has a task row while it is working. Devin
        remains the record either way, so the latest view is cached here rather
        than modelled.
        """
        self.set_meta("sessions", json.dumps(sessions))

    def sessions(self) -> list[dict[str, Any]]:
        raw = self.get_meta("sessions")
        return json.loads(raw) if raw else []
