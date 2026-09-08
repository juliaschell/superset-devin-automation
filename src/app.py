"""One FastAPI process: dashboard, metrics, report, and the reconcile loop.

One container, one process, one datastore. The loop is an asyncio task rather
than a second service because at a handful of findings a night there is nothing
for a second service to do except fail separately.
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

from . import metrics as metrics_mod
from .config import config
from .devin import DevinClient
from .github import GitHubClient
from .reconcile import Reconciler
from .replay import ReplayDevin, ReplayGitHub
from .store import Store

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_reconciler(store: Store) -> Reconciler:
    if config.live:
        devin: Any = DevinClient(config.devin_api_key, config.devin_org_id, config.devin_api_base)
        github: Any = GitHubClient(config.github_token, config.repo, config.github_api_base)
    else:
        devin, github = ReplayDevin(), ReplayGitHub()
    return Reconciler(store, devin, github, config)


def create_app() -> FastAPI:
    app = FastAPI(title="Superset remediation control plane")
    store = Store(config.db_path)
    reconciler = build_reconciler(store)
    app.state.store = store
    app.state.reconciler = reconciler

    async def loop() -> None:
        while True:
            await asyncio.to_thread(reconciler.cycle)
            await asyncio.sleep(config.poll_interval_seconds)

    @app.on_event("startup")
    async def _startup() -> None:
        store.log("service_start", detail=f"mode={config.mode} repo={config.repo}")
        app.state.task = asyncio.create_task(loop())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def current_metrics() -> dict[str, Any]:
        return metrics_mod.compute(store, config.build_acus, config.run_acus)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> Any:
        return templates.TemplateResponse(
            "dashboard.html",
            {
                "request": request,
                "metrics": current_metrics(),
                "tasks": store.tasks(),
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
        age = current_metrics()["last_reconciled_seconds_ago"]
        stale = age is None or age > config.poll_interval_seconds * 4
        # A dashboard that has stopped reconciling is worse than one that is
        # down, because it looks fine. Say so rather than lying.
        return JSONResponse(
            {"ok": not stale, "last_reconciled_seconds_ago": age, "mode": config.mode},
            status_code=200 if not stale else 503,
        )

    @app.post("/reconcile")
    def reconcile_now() -> Any:
        return JSONResponse(reconciler.cycle())

    return app


app = create_app()
