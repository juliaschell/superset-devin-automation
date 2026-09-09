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
    """Runtime configuration."""

    devin_api_key: str = field(default_factory=lambda: _env("DEVIN_API_KEY"))
    devin_api_base: str = field(default_factory=lambda: _env("DEVIN_API_BASE", "https://api.devin.ai"))
    devin_org_id: str = field(default_factory=lambda: _env("DEVIN_ORG_ID"))
    github_token: str = field(default_factory=lambda: _env("GITHUB_TOKEN"))
    github_api_base: str = field(default_factory=lambda: _env("GITHUB_API_BASE", "https://api.github.com"))
    repo: str = field(default_factory=lambda: _env("REPO"))
    session_tag: str = field(default_factory=lambda: _env("SESSION_TAG", "superset-remediation"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/state.db"))
    poll_interval_seconds: int = field(default_factory=lambda: _int("POLL_INTERVAL_SECONDS", 30))
    session_timeout_seconds: int = field(default_factory=lambda: _int("SESSION_TIMEOUT_SECONDS", 3 * 3600))
    ready_label: str = field(default_factory=lambda: _env("READY_LABEL", "devin:ready"))
    rejected_label: str = field(default_factory=lambda: _env("REJECTED_LABEL", "devin:rejected"))

    def missing(self) -> list[str]:
        """Credentials the service cannot run without."""
        return [
            name
            for name, value in (
                ("DEVIN_API_KEY", self.devin_api_key),
                ("DEVIN_ORG_ID", self.devin_org_id),
                ("GITHUB_TOKEN", self.github_token),
                ("REPO", self.repo),
            )
            if not value
        ]


config = Config()
