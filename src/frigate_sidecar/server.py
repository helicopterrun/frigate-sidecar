"""FastAPI app factory and uvicorn entry."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from frigate_sidecar import __version__
from frigate_sidecar.config import Settings, load_settings
from frigate_sidecar.frigate_api import FrigateClient
from frigate_sidecar.routes import analysis as analysis_routes
from frigate_sidecar.routes import faces as faces_routes
from frigate_sidecar.routes import fps_budget as fps_budget_routes
from frigate_sidecar.routes import health as health_routes
from frigate_sidecar.routes import motion as motion_routes
from frigate_sidecar.routes import score_histogram as score_histogram_routes
from frigate_sidecar.routes import triage as triage_routes

_PACKAGE_ROOT = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_ROOT / "templates"
_STATIC_DIR = _PACKAGE_ROOT / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="frigate-sidecar",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # One-shot probe at startup so the UI knows whether to surface Plus controls.
    # Best-effort: if Frigate is down right now, we just hide them.
    with FrigateClient(settings.frigate.base_url) as fc:
        app.state.plus_enabled = fc.plus_enabled()

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(health_routes.router)
    app.include_router(triage_routes.router)
    app.include_router(motion_routes.router)
    app.include_router(score_histogram_routes.router)
    app.include_router(fps_budget_routes.router)
    app.include_router(analysis_routes.router)
    app.include_router(faces_routes.router)
    return app


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.sidecar.bind_host,
        port=settings.sidecar.bind_port,
        log_level=settings.log_level.lower(),
    )
