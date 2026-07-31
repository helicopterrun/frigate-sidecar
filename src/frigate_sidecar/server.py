"""FastAPI app factory and uvicorn entry."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from frigate_sidecar import __version__
from frigate_sidecar.auth import FrigateAuthMiddleware
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

# The proxy's catch-all: everything registered before it is a route the sidecar
# owns and therefore gates behind a Frigate session (auth.py).
_PROXY_CATCH_ALL = "/{path:path}"

logger = logging.getLogger(__name__)


async def _scrub_generation_loop(app: FastAPI) -> None:
    """Continuous ~60s edge (docs spec §5.4 option (a)) -- NEVER hourly, per
    the spec's own blocking correction: an hourly cron reproduces the
    top-of-hour hole this cache exists to remove.

    Retention pruning rides along on its own slower cadence: it used to be
    reachable only from the CLI, so an unattended deployment kept every sheet
    it ever generated.
    """
    from frigate_sidecar.scrub.generator import generate_cycle, prune

    settings: Settings = app.state.settings
    interval = settings.scrub.generate_interval_s
    budget = settings.scrub.max_segments_per_cycle
    next_prune = time.time() + settings.scrub.prune_interval_s
    while True:
        caught_up = True
        try:
            results = await generate_cycle(settings, now=time.time())
            # A camera that used its whole per-cycle budget still has history
            # behind it. Sleeping the full interval anyway makes a cold backfill
            # spend most of its wall-clock idle; the live edge only ever needs a
            # handful of segments, so this changes nothing in steady state.
            caught_up = not any(r.get("segments", 0) >= budget for r in results)
        except Exception:
            logger.exception("scrub: generation cycle failed")
        if time.time() >= next_prune:
            next_prune = time.time() + settings.scrub.prune_interval_s
            try:
                result = await asyncio.to_thread(prune, settings)
                if result["sheets_deleted"] or result["buckets_deleted"]:
                    logger.info("scrub: retention prune %s", result)
            except Exception:
                logger.exception("scrub: retention prune failed")
        if caught_up:
            await asyncio.sleep(interval)
        else:
            await asyncio.sleep(0)  # yield, then keep catching up


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
        # `_check_scrub_inputs` has already said so loudly.
        return True


def _check_scrub_inputs(settings: Settings) -> None:
    """Log the misconfigurations that otherwise make the generator a silent no-op.

    Both of these produced zero output and zero log lines before: a
    `recordings_path` that doesn't resolve (the recordings volume simply isn't
    mounted where the sidecar looks) and a missing ffmpeg.
    """
    recordings = settings.frigate.recordings_path
    if not recordings.exists():
        logger.error(
            "scrub is enabled but frigate.recordings_path (%s) does not exist -- the "
            "generator will find no segments to sample. Check the recordings mount "
            "(docs/scrub-cache-and-proxy-spec.md §8.2).",
            recordings,
        )
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            logger.error(
                "scrub is enabled but %s is not on PATH -- no frames can be extracted", binary
            )


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # One-shot probe so the UI knows whether to surface Plus controls.
    # Best-effort and off the event loop: if Frigate is down or slow right now,
    # we just hide them rather than stalling startup on a sync HTTP call.
    async def _probe_plus() -> None:
        def _probe() -> bool:
            with FrigateClient(settings.frigate.base_url) as fc:
                return fc.plus_enabled()

        with contextlib.suppress(Exception):
            app.state.plus_enabled = await asyncio.to_thread(_probe)

    probe_task = asyncio.create_task(_probe_plus())

    task: asyncio.Task[None] | None = None
    if settings.scrub.enabled:
        _check_scrub_inputs(settings)
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
        for pending in (task, probe_task):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        client = getattr(app.state, "http_client", None)
        if client is not None:
            await client.aclose()


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
    app.state.plus_enabled = False

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

    # Everything registered so far is the sidecar's own surface and requires a
    # Frigate session; the proxy catch-all below must not (Frigate does its own
    # auth and its 401 has to reach the client).
    owned_routes = [r for r in app.routes if getattr(r, "path", None) != _PROXY_CATCH_ALL]
    app.add_middleware(FrigateAuthMiddleware, owned_routes=owned_routes)

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
