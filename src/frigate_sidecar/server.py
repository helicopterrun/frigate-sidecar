"""FastAPI app factory and uvicorn entry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
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
from frigate_sidecar.routes import placement as placement_routes
from frigate_sidecar.routes import proxy as proxy_routes
from frigate_sidecar.routes import score_histogram as score_histogram_routes
from frigate_sidecar.routes import scrub as scrub_routes
from frigate_sidecar.routes import toybox as toybox_routes
from frigate_sidecar.routes import triage as triage_routes

_PACKAGE_ROOT = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_ROOT / "templates"
_STATIC_DIR = _PACKAGE_ROOT / "static"

logger = logging.getLogger(__name__)


async def _scrub_generation_loop(app: FastAPI) -> None:
    """Continuous ~60s edge (docs spec §5.4 option (a)) -- NEVER hourly, per
    the spec's own blocking correction: an hourly cron reproduces the
    top-of-hour hole this cache exists to remove."""
    from frigate_sidecar.scrub.generator import generate_cycle

    settings: Settings = app.state.settings
    interval = settings.scrub.generate_interval_s
    while True:
        try:
            await generate_cycle(settings, now=time.time())
        except Exception:
            logger.exception("scrub: generation cycle failed")
        await asyncio.sleep(interval)


def _cache_on_separate_filesystem(cache_dir: Path, recordings_path: Path) -> bool:
    """§8.3 hard requirement: refuse to enable scrub if its cache would land
    on the same filesystem as Frigate's recordings (which may be nearly
    full)."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return os.stat(cache_dir).st_dev != os.stat(recordings_path).st_dev
    except OSError:
        # Can't tell (e.g. recordings_path doesn't exist in this environment,
        # such as under test) -- don't block startup on an inconclusive check.
        return True


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    task: asyncio.Task[None] | None = None
    if settings.scrub.enabled:
        if not _cache_on_separate_filesystem(
            settings.scrub.cache_dir, settings.frigate.recordings_path
        ):
            logger.error(
                "scrub.cache_dir (%s) is on the same filesystem as "
                "frigate.recordings_path (%s) -- refusing to start the generator "
                "(docs spec §8.3)",
                settings.scrub.cache_dir, settings.frigate.recordings_path,
            )
        else:
            task = asyncio.create_task(_scrub_generation_loop(app))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="frigate-sidecar",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
        lifespan=_lifespan,
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
    app.include_router(placement_routes.router)
    app.include_router(analysis_routes.router)
    app.include_router(faces_routes.router)
    app.include_router(toybox_routes.router)
    app.include_router(scrub_routes.router)
    # Proxy is a catch-all (/{path:path}) and MUST be registered last so every
    # other route -- /v1/*, /static, /healthz, the sidecar's own pages -- wins
    # first (docs/scrub-cache-and-proxy-spec.md §6).
    app.include_router(proxy_routes.router)
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
