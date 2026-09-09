"""Configuration, all from the environment. No secrets are ever written to disk."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    """Runtime configuration.

    ``mode`` is the one switch that matters:

    - ``live``   — poll the real Devin and GitHub APIs.
    - ``replay`` — read a recorded run from ``demo/recorded_run.json``. No
      credentials, no network. This is what ``make demo`` uses.
    """

    mode: str = field(default_factory=lambda: _env("MODE", "replay"))
    devin_api_key: str = field(default_factory=lambda: _env("DEVIN_API_KEY"))
    devin_api_base: str = field(default_factory=lambda: _env("DEVIN_API_BASE", "https://api.devin.ai"))
    devin_org_id: str = field(default_factory=lambda: _env("DEVIN_ORG_ID"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_api_base: str = field(default_factory=lambda: _env("GITHUB_API_BASE", "https://api.github.com"))
    repo: str = field(default_factory=lambda: _env("REPO", "juliaschell/superset"))
    session_tag: str = field(default_factory=lambda: _env("SESSION_TAG", "superset-remediation"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/state.db"))
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 30))
    session_timeout_seconds: int = field(default_factory=lambda: _int("SESSION_TIMEOUT_SECONDS", 3 * 3600))
    ready_label: str = field(default_factory=lambda: _env("READY_LABEL", "devin:ready"))
    rejected_label: str = field(default_factory=lambda: _env("REJECTED_LABEL", "devin:rejected"))

    @property
    def live(self) -> bool:
        return self.mode == "live"


config = Config()
