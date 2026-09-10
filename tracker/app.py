"""One FastAPI process: the watch loop, the dashboard, and its endpoints.

The loop is an asyncio task rather than a second service because at a handful
of findings a night, a second service has nothing to do but fail separately.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from shared.config import config
from shared.devin import DevinClient
from shared.github import GitHubClient

from . import metrics as metrics_mod
from .store import Store
from .watch import Watcher

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_watcher(store: Store) -> Watcher:
    if missing := config.missing():
        raise SystemExit(
            f"missing required configuration: {', '.join(missing)} — pass them to make, "
            "e.g. make up REPO=you/superset DEVIN_KEY=... DEVIN_ORG=... GITHUB_TOKEN=..."
        )
    devin = DevinClient(config.devin_api_key, config.devin_org_id, config.devin_api_base)
    github = GitHubClient(config.github_token, config.repo, config.github_api_base)
    return Watcher(store, devin, github, config)


def create_app() -> FastAPI:
    app = FastAPI(title="Superset remediation tracker")
    store = Store(config.db_path)
    watcher = build_watcher(store)
    # After the configuration is validated, so an empty REPO never becomes the
    # repo this database claims to be about.
    store.bind_repo(config.repo)
    app.state.store = store
    app.state.watcher = watcher

    async def loop() -> None:
        while True:
            await asyncio.to_thread(watcher.cycle)
            await asyncio.sleep(config.poll_interval_seconds)

    @app.on_event("startup")
    async def _startup() -> None:
        store.log("service_start", detail=f"repo={config.repo}")
        app.state.task = asyncio.create_task(loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def current_metrics() -> dict[str, Any]:
        return metrics_mod.compute(store)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "metrics": current_metrics(),
                "tasks": store.tasks(),
                "sessions": store.sessions(),
                "events": store.events(limit=40),
                "config": config,
            },
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def prometheus_metrics() -> str:
        return metrics_mod.prometheus(current_metrics())

    @app.get("/metrics.json")
    def json_metrics() -> Any:
        return JSONResponse(current_metrics())

    @app.get("/report.md", response_class=PlainTextResponse)
    def report() -> str:
        return metrics_mod.report_markdown(current_metrics(), store.tasks(), config.repo)

    @app.get("/healthz")
    def healthz() -> Any:
        age = current_metrics()["last_checked_seconds_ago"]
        stale = age is None or age > config.poll_interval_seconds * 4
        # A dashboard that has stopped updating is worse than one that is down,
        # because it still looks fine.
        return JSONResponse(
            {"ok": not stale, "last_checked_seconds_ago": age, "repo": config.repo},
            status_code=200 if not stale else 503,
        )

    @app.post("/check")
    def check_now() -> Any:
        return JSONResponse(watcher.cycle())

    return app


app = create_app()
