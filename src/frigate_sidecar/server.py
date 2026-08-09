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
from frigate_sidecar.push import delivery, delivery_wire
from frigate_sidecar.push import store as push_store
from frigate_sidecar.push.engine import PushEngine
from frigate_sidecar.push.mqtt import MqttReviewSubscriber
from frigate_sidecar.push.transport import LogTransport, RelayTransport
from frigate_sidecar.routes import analysis as analysis_routes
from frigate_sidecar.routes import debug as debug_routes
from frigate_sidecar.routes import devices as devices_routes
from frigate_sidecar.routes import faces as faces_routes
from frigate_sidecar.routes import fps_budget as fps_budget_routes
from frigate_sidecar.routes import health as health_routes
from frigate_sidecar.routes import motion as motion_routes
from frigate_sidecar.routes import placement as placement_routes
from frigate_sidecar.routes import proxy as proxy_routes
from frigate_sidecar.routes import push as push_routes
from frigate_sidecar.routes import score_histogram as score_histogram_routes
from frigate_sidecar.routes import scrub as scrub_routes
from frigate_sidecar.routes import scrub_ui as scrub_ui_routes
from frigate_sidecar.routes import status as status_routes
from frigate_sidecar.routes import toybox as toybox_routes
from frigate_sidecar.routes import triage as triage_routes
from frigate_sidecar.routes import zone_hits as zone_hits_routes

_PACKAGE_ROOT = Path(__file__).parent
_TEMPLATES_DIR = _PACKAGE_ROOT / "templates"
_STATIC_DIR = _PACKAGE_ROOT / "static"

# The proxy's catch-all: everything registered before it is a route the sidecar
# owns and therefore gates behind a Frigate session (auth.py).
_PROXY_CATCH_ALL = "/{path:path}"

logger = logging.getLogger(__name__)


async def _scrub_generation_loop(app: FastAPI) -> None:
    """Continuous trailing edge (docs spec §5.4 option (a)) -- NEVER hourly, per
    the spec's own blocking correction: an hourly cron reproduces the
    top-of-hour hole this cache exists to remove.

    The tick is `live_edge_interval_s`, and it is a *deadline*, not a sleep: the
    trailing-window pass runs at the top of every tick and backfill gets the time
    left over. It used to be the other way round -- a full cycle, then a fixed
    sleep -- so backfill's budget landed on top of a live-edge pass that had
    grown to ~65s and pushed the effective cadence to ~100s. Cadence is the floor
    on how stale the newest cell can be, so that came straight off the freshness
    the client is promised. Nothing here bounds *throughput*; the same segments
    are decoded either way, in smaller instalments.

    A tick that overruns is not slept off -- backfill is simply cut, and if the
    live pass alone overran, the next tick starts immediately. That is the
    correct priority: history can wait, the edge cannot.

    Retention pruning rides along on its own slower cadence: it used to be
    reachable only from the CLI, so an unattended deployment kept every sheet
    it ever generated.
    """
    from frigate_sidecar.scrub.generator import SourceProfile, generate_cycle, prune

    settings: Settings = app.state.settings
    # `generate_interval_s` remains the ceiling, so a deployment that has
    # deliberately slowed generation down keeps that setting meaningful.
    tick = min(settings.scrub.generate_interval_s, settings.scrub.live_edge_interval_s)
    next_prune = time.time() + settings.scrub.prune_interval_s
    # Measured GOP and aspect per camera, kept for the process lifetime.
    profile = SourceProfile()
    while True:
        deadline = time.monotonic() + tick
        try:
            await generate_cycle(
                settings, now=time.time(), profile=profile, backfill_deadline=deadline
            )
        except Exception:
            logger.exception("scrub: generation cycle failed")
        else:
            # Consumed by the status dashboard: "when did a cycle last finish".
            app.state.scrub_last_cycle = time.time()
        if time.time() >= next_prune:
            next_prune = time.time() + settings.scrub.prune_interval_s
            try:
                result = await asyncio.to_thread(prune, settings)
                if any(v for k, v in result.items() if k.endswith("_deleted")):
                    logger.info("scrub: retention prune %s", result)
            except Exception:
                logger.exception("scrub: retention prune failed")
        # Zero when the tick overran, which keeps the edge pass running
        # back-to-back on a cache that is still catching up.
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))


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


def _build_push_transport(settings: Settings):  # noqa: ANN201 - Protocol return
    """Mock/log transport by default -- the only one usable without real APNs
    credentials (spec §4). "relay" posts to `push.relay_base_url`."""
    if settings.push.transport == "relay":
        return RelayTransport(settings.push.relay_base_url, timeout=settings.push.relay_timeout_s)
    return LogTransport()


async def _push_subscriber_loop(app: FastAPI) -> None:
    subscriber: MqttReviewSubscriber = app.state.push_subscriber
    try:
        await subscriber.run_forever()
    except Exception:
        logger.exception("push: mqtt subscriber loop crashed")


async def _activity_sweep_loop(app: FastAPI) -> None:
    """End Live Activities whose situation has gone quiet.

    Resolution is the one transition nothing announces -- no message arrives
    to say "the object stopped being reported" -- so it needs a sweep. This
    only ever *ends* activities; it never refreshes one, so the rule that
    stage transitions are the only thing minting an LA push still holds.
    """
    engine: PushEngine = app.state.push_engine
    interval = app.state.settings.push.activity_sweep_interval_s
    while True:
        await asyncio.sleep(interval)
        try:
            await engine.sweep_activities()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("push: activity sweep failed")


async def _delivery_resound_sweep_loop(app: FastAPI) -> None:
    """The urgent-only re-sound timer (design doc §3): an `urgent` card
    still unhandled after `delivery_urgent_resound_s` gets exactly one more
    sound. Only ever adds a sound to a card that already exists and already
    sounded once -- it never creates or ends anything, so it's the same
    kind of "only tightens, never a keep-alive" sweep as
    `_activity_sweep_loop`.
    """
    engine: PushEngine = app.state.push_engine
    settings: Settings = app.state.settings
    interval = settings.push.delivery_resound_sweep_interval_s
    while True:
        await asyncio.sleep(interval)
        try:
            from frigate_sidecar import db

            conn = db.open_sidecar(engine.db_path)
            try:
                devices = push_store.list_devices(conn)
                await delivery.sweep_urgent_resound(
                    conn, engine.transport, devices,
                    interval_s=settings.push.delivery_urgent_resound_s,
                    enabled=settings.push.delivery_urgent_resound_enabled,
                    payload_for_resound=delivery_wire.resound_payload_for,
                )
            finally:
                conn.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("push: delivery re-sound sweep failed")


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

    push_task: asyncio.Task[None] | None = None
    sweep_task: asyncio.Task[None] | None = None
    delivery_sweep_task: asyncio.Task[None] | None = None
    if settings.push.enabled:
        from frigate_sidecar import db
        from frigate_sidecar.push import card_store, policy_settings

        _conn = db.open_sidecar(str(settings.sidecar.db_path))
        try:
            collapsed = card_store.migrate_drop_zone_from_card_keys(_conn)
            if collapsed:
                logger.info(
                    "push: collapsed %d zone-bearing card row(s) onto the "
                    "camera/subject-kind/track-id identity", collapsed,
                )
        finally:
            _conn.close()

        # Load the user-editable routing/zone/LA policy (Elsinore Phase 4)
        # and apply it before the first card can ever be evaluated --
        # `ladder_policy.TABLE` must never be read in its own unmodified
        # default state on a deployment that has a settings file.
        policy_settings.startup(settings.push.push_settings_path)

        server_id = settings.push.server_id or f"s_{id(app):x}"
        transport = _build_push_transport(settings)
        app.state.push_transport = transport
        engine = PushEngine(
            db_path=str(settings.sidecar.db_path),
            transport=transport,
            server_id=server_id,
            handle_ttl_s=settings.push.handle_ttl_s,
            situation_handle_ttl_s=settings.push.situation_handle_ttl_s,
            frigate_base_url=settings.frigate.base_url,
            rate_limit_per_hour=settings.push.rate_limit_per_hour,
            rate_limit_window_s=settings.push.rate_limit_window_s,
            thumbnail_max_edge=settings.push.thumbnail_max_edge,
            thumbnail_quality=settings.push.thumbnail_quality,
            thumbnail_timeout_s=settings.push.thumbnail_timeout_s,
            dwell_source=settings.push.dwell_source,
            activity_update_min_interval_s=settings.push.activity_update_min_interval_s,
            activity_resolution_s=settings.push.activity_resolution_s,
            activity_dismissal_tail_s=settings.push.activity_dismissal_tail_s,
            activity_updates_per_hour=settings.push.activity_updates_per_hour,
            activity_reap_after_s=settings.push.activity_reap_after_s,
            push_config=settings.push,
        )
        app.state.push_engine = engine
        subscriber = MqttReviewSubscriber(
            settings.push, engine, frigate_base_url=settings.frigate.base_url
        )
        app.state.push_subscriber = subscriber
        push_task = asyncio.create_task(_push_subscriber_loop(app))
        sweep_task = asyncio.create_task(_activity_sweep_loop(app))
        if settings.push.delivery_enabled:
            delivery_sweep_task = asyncio.create_task(_delivery_resound_sweep_loop(app))

    try:
        yield
    finally:
        for pending in (task, probe_task, push_task, sweep_task, delivery_sweep_task):
            if pending is not None:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        running_subscriber = getattr(app.state, "push_subscriber", None)
        if running_subscriber is not None:
            running_subscriber.stop()
        running_engine = getattr(app.state, "push_engine", None)
        if running_engine is not None:
            await running_engine.aclose()
        transport = getattr(app.state, "push_transport", None)
        if isinstance(transport, RelayTransport):
            await transport.aclose()
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
    app.include_router(status_routes.router)
    app.include_router(scrub_ui_routes.router)
    app.include_router(debug_routes.router)
    app.include_router(devices_routes.router)
    app.include_router(zone_hits_routes.router)
    app.include_router(triage_routes.router)
    app.include_router(motion_routes.router)
    app.include_router(score_histogram_routes.router)
    app.include_router(fps_budget_routes.router)
    app.include_router(placement_routes.router)
    app.include_router(analysis_routes.router)
    app.include_router(faces_routes.router)
    app.include_router(toybox_routes.router)
    app.include_router(scrub_routes.router)
    app.include_router(push_routes.router)

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
    # uvicorn's log_level only configures uvicorn's own loggers; the root logger
    # keeps its WARNING default, so everything the sidecar itself logs below
    # that -- the per-cycle scrub telemetry in particular -- was written and
    # then dropped. The watchdog entry point has always done this; the server
    # never did.
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs a line per request at INFO. Every proxied request goes through
    # it, so at this cadence that is thousands of lines an hour burying anything
    # the sidecar has to say (the watchdog quiets it for the same reason).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        create_app(settings),
        host=settings.sidecar.bind_host,
        port=settings.sidecar.bind_port,
        log_level=settings.log_level.lower(),
    )
