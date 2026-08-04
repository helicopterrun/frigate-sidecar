"""Sidecar configuration: YAML file + env-var overrides.

Loading precedence (highest wins):
1. Environment variables prefixed FRIGATE_SIDECAR_ (nested with __).
2. YAML file at FRIGATE_SIDECAR_CONFIG, or the default search path.
3. Defaults defined on the models below.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_CONFIG_PATHS = (
    "/etc/frigate-sidecar/sidecar.yml",
    "./config/sidecar.yml",
)

logger = logging.getLogger(__name__)


class FrigateSection(BaseModel):
    base_url: str = "http://frigate.lan:5000"
    config_path: Path = Path("/opt/frigate/config.yml")
    db_path: Path = Path("/opt/frigate/database/frigate.db")
    # Authed origin used ONLY to proxy app traffic (routes/proxy.py). Auth stays
    # entirely Frigate's — the sidecar forwards the client's own cookie and never
    # holds a password. Deliberately separate from base_url (unauth, sidecar's
    # own server-to-server calls) — do not merge them (docs/scrub-cache-and-proxy-spec.md §3.2).
    proxy_base_url: str = "http://frigate.lan:8971"
    # DB's container-side recordings root, as stored in recordings.path. Used
    # only to strip this prefix before reattaching recordings_path (§8.2).
    media_path: Path = Path("/media/frigate")
    # Host-side path that `media_path` is REPLACED BY, not the recordings tree
    # root. `recordings.path` is `<media_path>/recordings/<date>/...`, so the
    # `recordings/` segment comes from the DB value and must not be repeated
    # here: pointing this at .../recordings/recordings mapped every segment to a
    # path one level too deep, every file lookup missed, and generation produced
    # nothing at all (§8.2 M6). Verify with:
    #   python -c "from frigate_sidecar.scrub.mapping import map_recording_path"
    # against a real `recordings.path` row before trusting a new value.
    recordings_path: Path = Path("/mnt/frigate-storage/recordings")


class SidecarSection(BaseModel):
    db_path: Path = Path("/data/frigate-sidecar.db")
    bind_host: str = "0.0.0.0"
    bind_port: int = 5001
    # Every endpoint the sidecar owns -- the triage UI, /faces, /analysis,
    # /toybox, /v1 -- requires the same Frigate session cookie the proxy
    # already forwards to Frigate. On by default: the sidecar sits on the same
    # LAN origin as Frigate and exposes event history, face crops and
    # label/promote writes, so leaving it open makes it a bypass of Frigate's
    # own auth. The proxy catch-all is deliberately NOT gated here (Frigate
    # authenticates that traffic itself, and its 401 must reach the client).
    # Set false only on a deployment where Frigate's own auth is disabled.
    require_frigate_auth: bool = True
    # A cookie that validated upstream is trusted this long before being
    # re-checked, so /v1 doesn't add a round-trip per request.
    auth_cache_ttl_s: float = 60.0
    # Hard cap on remembered sessions (Frigate rotates its JWT, so the key
    # space is unbounded without one).
    auth_cache_max_entries: int = 1024


class FaceSection(BaseModel):
    """Face-training-image quality curation (B1).

    Scores Frigate's auto-saved face crops and promotes the good ones into the
    named Face Library via Frigate's API. `auto_promote` starts off so the
    first runs are observe-only — flip it on after reviewing the quality
    histogram.
    """

    enabled: bool = False
    # Frigate's clips/faces dir as seen from the sidecar host (LXC 105), not the
    # container path. Holds the `train/` attempt pool + per-person library dirs.
    clips_faces_dir: Path = Path("/mnt/frigate-storage/recordings/clips/faces")
    auto_promote: bool = False
    quality_threshold: float = 0.0  # min combined quality_score to auto-promote
    min_recog_score: float = 0.9  # only auto-promote crops Frigate recognized this well
    per_person_cap: int = 40  # don't let auto-promote overgrow one person's library


class WatchdogSection(BaseModel):
    """External health watchdog for the Frigate container.

    Polls Frigate's HTTP API and restarts the container when its backend hangs
    — connection-refused or repeated 5xx. That's the failure mode Docker's own
    restart policy can't catch: Frigate's main process can wedge on a frozen
    camera stream while its s6 PID 1 stays alive, so the container reads "Up"
    but every /api/* request 500s through nginx. Off by default; runs as its
    own process via contrib/frigate-watchdog.service (not inside the web app,
    so it survives even if uvicorn's event loop is blocked).
    """

    enabled: bool = False
    # Probed as frigate.base_url + probe_path. /api/version is the cheapest
    # endpoint that still 500s when the backend is hung; it returns 200 even in
    # safe mode, so a bad-config safe-mode boot will NOT trigger a restart loop.
    probe_path: str = "/api/version"
    interval_s: float = 30.0
    timeout_s: float = 10.0
    # Consecutive failed probes before a restart. 4 × 30s = ~2 min of sustained
    # failure, so a brief blip or a single slow probe won't trip it.
    failures_before_restart: int = 4
    restart_command: list[str] = Field(
        default_factory=lambda: ["docker", "restart", "frigate"]
    )
    restart_timeout_s: float = 120.0
    # After a restart, ignore failures for this long so Frigate's boot (during
    # which probes naturally fail) can't trigger a second restart mid-startup.
    cooldown_s: float = 180.0
    # Safety cap: if Frigate is fundamentally broken, stop hammering it and log
    # loudly for manual intervention instead of restart-looping forever.
    max_restarts_per_hour: int = 3


class ScrubSection(BaseModel):
    """Uniform-cadence sprite-sheet scrub cache (docs/scrub-cache-and-proxy-spec.md).

    Off by default; opt-in per deployment. `retention_days` is capped by how
    long continuous (non-motion-only) recording actually lasts on this
    deployment -- measured at ~4 days, not the record.retain.days config value.
    """

    enabled: bool = False
    cameras: list[str] = Field(default_factory=list)  # [] = all cameras
    cache_dir: Path = Path("/data/scrub")  # MUST be a separate filesystem from
    # frigate.recordings_path -- verified at startup, see routes/scrub.py.
    recent_interval_s: float = 1.0
    aged_interval_s: float = 5.0
    # Generate a camera at its own keyframe cadence when that is coarser than
    # `recent_interval_s`, instead of full-decoding to force the configured
    # rate. A source whose GOP is longer than the target interval can only hit
    # that interval by decoding every frame -- measured at ~5x the cost of
    # keyframe extraction, and on the reference deployment the three UniFi
    # Protect cameras (5s GOP, against 1s on the seven Dahua ones) accounted for
    # roughly 70% of the generator's total work while being 30% of the fleet.
    # The cadence is per bucket and travels to the client in `interval`, so a
    # camera generating at 5s is contract-compatible; it just yields a still
    # every 5s rather than every second. Turn off to force the configured
    # interval everywhere and pay the decode.
    match_keyframe_cadence: bool = True
    # Extra tiers, each overlapping the recent+aged tiers rather than replacing
    # any of them: each covers the *entire* retention window at its own
    # coarser cadence, so a client can render a whole-history scrubber without
    # paying for retention_days worth of `aged_interval_s` sheets. An empty
    # list disables this -- the default keeps a 10s tier (a reasonable
    # "scrub a day back" cadence) and a 60s tier (a "scrub the whole
    # retention window" cadence) on top of today's two-tier, non-overlapping
    # recent/aged behaviour, which is otherwise unchanged.
    coarse_intervals_s: list[float] = Field(default_factory=lambda: [10.0, 60.0])
    aged_after_h: float = 24.0
    retention_days: int = 4
    cell_w: int = 320
    # Fallback height, used only when the source's shape can't be measured or
    # `preserve_source_aspect` is off. Otherwise the height is derived per
    # camera from the source's display aspect ratio.
    cell_h: int = 180
    # Derive each camera's cell height from its own aspect ratio instead of
    # scaling every source into a fixed cell. A 4:3 camera rendered into a 16:9
    # cell comes out anamorphically squeezed, and nothing downstream can undo it
    # -- the pixels are already wrong. Two of the ten cameras here are 1600x1200.
    # The resulting dimensions travel per sheet in the `cell_w`/`cell_h`
    # metadata, so a client reading those renders each camera correctly.
    preserve_source_aspect: bool = True
    sheet_cols: int = 12
    sheet_rows: int = 8
    format: str = "jpeg"  # "jpeg" | "webp" -- JPEG measured smaller on real
    # camera content (see docs spec §5.3 M4); WebP requires -lossless 0.
    generate_interval_s: float = 60.0  # continuous edge, NOT hourly (§5.4)
    # How often the trailing-window pass runs, and therefore the generation
    # loop's tick. This is the floor on how stale the newest sprite cell can be,
    # because a camera serviced at the start of one tick is not touched again
    # until the next.
    #
    # It exists because the tick used to be `generate_interval_s` with the
    # backfill phase inside it: backfill's own budget landed on top of a
    # live-edge pass that had grown to ~65s (it feeds the coarse tiers now), so
    # the effective cadence was ~100s and measured lag ~105s -- past the ~90s
    # the client is told to expect, and past it *further* whenever a slow
    # segment stretched the cycle. Backfill is now bounded by the next tick
    # rather than the tick being bounded by backfill.
    #
    # Throughput is unchanged by shortening it: the same segments are decoded
    # either way, just in smaller instalments, so latency improves at equal CPU.
    # What it does cost is sheet versions -- a still-filling sheet is published
    # once per tick, and every version is its own immutable object (§4.3) --
    # which is what `sheet_version_grace_s` sweeps back up.
    live_edge_interval_s: float = 20.0
    # How long a *superseded* still-filling sheet version stays servable after a
    # larger version of the same sheet is published. Complete sheets are never
    # superseded and are never swept by this; retention alone removes those.
    #
    # Without a sweep, a 96-cell 1s sheet publishes ~5 growing versions at the
    # default tick and all of them live until retention: measured at ~1.1 MB for
    # a full sheet on real footage, that is roughly 3x the tier's steady-state
    # size, on the filesystem §8.3 goes out of its way to keep free. The grace
    # window is what keeps the sweep safe -- a client holding a URL from its last
    # index fetch still resolves it; one holding a 15-minute-old URL gets a 404
    # and falls back, which is the same path it already takes for a span with no
    # coverage.
    sheet_version_grace_s: float = 900.0
    # Retention sweep cadence for the in-process generator. Pruning used to be
    # CLI-only, so an unattended deployment grew past retention_days forever.
    prune_interval_s: float = 3600.0
    # Bounds concurrent ffmpeg/ffprobe children. Segments within a camera are
    # sampled serially (cell assignment is order-dependent), so today this only
    # matters if a CLI backfill runs alongside the in-process generator.
    ffmpeg_concurrency: int = 3
    # Backfill allowance for a whole cycle, split across cameras. Without any
    # cap the first cycle on a cold cache tries to sample the whole retention
    # horizon -- days of ffmpeg -- before the loop comes up for air.
    backfill_segments_per_cycle: int = 120
    # Wall-clock ceiling on the backfill phase. The segment count alone can't
    # bound the cycle, because how long a segment takes depends on the box; and
    # an over-long cycle delays the next live-edge pass, which is what let the
    # edge slip behind in the first place. Holding the edge for ten cameras at
    # 1 fps already costs most of a core, so backfill takes genuine leftovers
    # and no more.
    #
    # Cycle length is the floor on live-edge lag: a camera serviced at the start
    # of a cycle is a full cycle stale by the end of it. Measured on this
    # deployment, 35s here settled at a 100s cycle and ~105s lag -- over the 90s
    # the client is told to expect. 22s settles inside it. That is not the old
    # 20s in disguise: one decode now feeds every tier, so the same wall clock
    # buys several times the coverage it used to.
    backfill_time_budget_s: float = 22.0
    # Cap on the live-edge pass, per camera per cycle. Sized to cover the whole
    # lookback in one pass (900s / 10s segments), so a camera reaches `now` in a
    # single cycle rather than converging over several: a fixed small cap loses
    # ground whenever the cycle takes longer than the footage it generated, which
    # is exactly what happens once backfill shares the cycle.
    live_edge_segments: int = 90
    # How far back the live-edge pass will resume from. A cache that is further
    # behind than this jumps forward to the edge and leaves the gap for
    # backfill: crawling up from a day ago meant nothing recent was ever
    # generated, which is the one window clients actually scrub.
    live_edge_lookback_s: float = 900.0

    @field_validator("format")
    @classmethod
    def _known_format(cls, v: str) -> str:
        fmt = v.strip().lower()
        if fmt not in ("jpeg", "webp"):
            raise ValueError(f"scrub.format must be 'jpeg' or 'webp', got {v!r}")
        return fmt

    @field_validator("live_edge_interval_s", "sheet_version_grace_s")
    @classmethod
    def _positive(cls, v: float) -> float:
        """A non-positive tick would spin the generation loop without yielding,
        and a non-positive grace would sweep a version the index is advertising
        in the same breath it publishes it.
        """
        if v <= 0:
            raise ValueError(f"must be > 0, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_coarse_intervals(self) -> ScrubSection:
        """Every entry in `coarse_intervals_s` must be strictly coarser than
        `aged_interval_s` (otherwise it isn't an extra tier, just a duplicate
        of the aged one), distinct from every other entry (otherwise two
        tiers would generate and serve identical buckets under the same
        interval, silently clobbering each other), and land on the same
        epoch-anchored grid every interval uses (`grid.decimate_to_grid`,
        `grid.grid_point`): bucket/slot boundaries are `k * interval` from
        absolute epoch zero, so an interval that isn't a whole multiple of
        `aged_interval_s` puts that tier's grid points out of step with the
        aged tier's at every boundary but the first.
        """
        seen: set[float] = set()
        for coarse in self.coarse_intervals_s:
            if coarse in seen:
                raise ValueError(
                    f"scrub.coarse_intervals_s must not repeat a value (got {coarse!r} twice)"
                )
            seen.add(coarse)
            if coarse <= self.aged_interval_s:
                raise ValueError(
                    "scrub.coarse_intervals_s entries must each be > scrub.aged_interval_s "
                    f"(got coarse={coarse!r}, aged={self.aged_interval_s!r})"
                )
            ratio = coarse / self.aged_interval_s
            if abs(ratio - round(ratio)) > 1e-6:
                raise ValueError(
                    "scrub.coarse_intervals_s entries must each be a whole multiple of "
                    f"scrub.aged_interval_s to land on its epoch grid "
                    f"(got coarse={coarse!r}, aged={self.aged_interval_s!r})"
                )
        return self


class ProxySection(BaseModel):
    enabled: bool = True
    pass_request_headers: list[str] = Field(
        default_factory=lambda: ["range", "authorization", "cookie"]
    )


class PushSection(BaseModel):
    """Push notifications (docs/push-notifications.md).

    Off by default -- push is the last capability tier to light up, never a
    dependency of any other one (spec's "optional always" non-negotiable). The
    sidecar is the only APNs-facing piece; devices register against it the same
    way they authenticate against everything else (§1: reuse the sidecar's
    existing Frigate-session auth, no second credential).
    """

    enabled: bool = False
    # "mock" logs what would be sent and always succeeds -- the only transport
    # available without real APNs credentials, and the default so a fresh
    # deployment doesn't accidentally try to reach a relay that isn't there.
    # "relay" posts the minimal {device_token, environment, handle, server_id,
    # severity} payload to `relay_base_url` (spec §4).
    transport: str = "mock"
    # Short opaque id of *this* sidecar instance, carried in the APNs payload
    # so a device with more than one server registered can route the NSE's
    # handle-redeem fetch to the right base URL (spec §2). Generated at
    # startup if left blank -- see push/engine.py.
    server_id: str = ""

    # -- MQTT (event source, spec's "Architecture at a glance") --
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "frigate-sidecar-push"
    mqtt_topic_reviews: str = "frigate/reviews"
    mqtt_topic_available: str = "frigate/available"
    # Reconnect backoff (spec §5, "MQTT broker unreachable from the sidecar").
    reconnect_backoff_s: float = 2.0
    reconnect_backoff_max_s: float = 60.0
    # After this long without any broker traffic, treat Frigate as possibly
    # offline and back-fill the gap on reconnect/resume (spec's stale/live
    # model, §12.6, reused verbatim) rather than silently dropping alerts.
    offline_silence_s: float = 60.0
    backfill_lookback_s: float = 60.0

    # -- Relay transport (spec §4) --
    # The deployed shared relay (github.com/helicopterrun/elsinore-push-relay):
    # holds the one team-bound APNs key and forwards content-free templated
    # alerts. Overridable for forks running their own relay under their own
    # bundle id/team.
    relay_base_url: str = "https://elsinore-push-relay.helicopterrun.workers.dev"
    relay_timeout_s: float = 10.0

    # -- Handle redemption (spec §3 step 2) --
    handle_ttl_s: float = 3600.0

    @field_validator("transport")
    @classmethod
    def _known_transport(cls, v: str) -> str:
        t = v.strip().lower()
        if t not in ("mock", "relay"):
            raise ValueError(f"push.transport must be 'mock' or 'relay', got {v!r}")
        return t


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FRIGATE_SIDECAR_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    frigate: FrigateSection = Field(default_factory=FrigateSection)
    sidecar: SidecarSection = Field(default_factory=SidecarSection)
    face: FaceSection = Field(default_factory=FaceSection)
    watchdog: WatchdogSection = Field(default_factory=WatchdogSection)
    scrub: ScrubSection = Field(default_factory=ScrubSection)
    proxy: ProxySection = Field(default_factory=ProxySection)
    push: PushSection = Field(default_factory=PushSection)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_origins(self) -> Settings:
        """Warn (don't fail) when both Frigate origins are the same.

        `base_url` is the unauthenticated origin the sidecar calls itself;
        `proxy_base_url` is the authenticated one it forwards client traffic
        to and validates sessions against. Pointing both at the unauthenticated
        port silently turns the `/v1` session check into a no-op -- any cookie
        would pass -- so it's worth a loud line in the log even though a
        Frigate install with auth disabled is a legitimate configuration.
        """
        if self.frigate.base_url.rstrip("/") == self.frigate.proxy_base_url.rstrip("/"):
            logger.warning(
                "frigate.base_url and frigate.proxy_base_url are identical (%s) -- if that "
                "origin does not require a Frigate session, the sidecar's own auth check "
                "cannot reject anything (docs/scrub-cache-and-proxy-spec.md §3.2)",
                self.frigate.base_url,
            )
        return self


class _StaticYamlSource(PydanticBaseSettingsSource):
    """A pydantic-settings source backed by a pre-loaded YAML dict.

    Implemented as a settings source (not as init kwargs) so env variables
    can still override YAML values — env_settings runs first in the source
    tuple returned by `settings_customise_sources`.
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def _discover_yaml_path(explicit: str | os.PathLike[str] | None) -> Path | None:
    if explicit:
        return Path(explicit)
    if env_path := os.environ.get("FRIGATE_SIDECAR_CONFIG"):
        return Path(env_path)
    for candidate in DEFAULT_CONFIG_PATHS:
        p = Path(candidate)
        if p.exists():
            return p
    return None


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    yaml_path = _discover_yaml_path(config_path)
    yaml_data = _read_yaml(yaml_path) if yaml_path else {}

    class _BoundSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                init_settings,
                env_settings,
                _StaticYamlSource(settings_cls, yaml_data),
                file_secret_settings,
            )

    return _BoundSettings()
