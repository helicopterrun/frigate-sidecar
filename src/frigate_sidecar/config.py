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
    # Allow POST /v1/push/frigate-config/refresh to overwrite `config_path`.
    # Default OFF: on the bare-metal install config_path is Frigate's LIVE
    # config.yml, and a refresh would replace it wholesale. Only enable on a
    # deployment where config_path is a sidecar-owned snapshot (dev).
    config_refresh_enabled: bool = False
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
    # "Stay signed in" lifetime for the sidecar's own remember-me cookie,
    # minted by POST /login/remember after a successful Frigate login. The
    # sidecar never stores the password — the cookie is a signed expiry
    # token, so revocation is "wait for expiry or rotate the secret file".
    remember_ttl_s: float = 30 * 86400.0


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
    # Tiers derived from the decode tiers (recent/aged) rather than sampled
    # with ffmpeg: each is generated by picking every Nth already-published
    # cell out of whichever decode tier is finest over a given span, and
    # re-tiling -- cheap disk I/O and PIL crops, no decode cost at all. Runs
    # last in each generation cycle (after live-edge and backfill), out of
    # whatever's left of the tick's deadline. An empty list disables this. The
    # default keeps 60s ("scrub a day back"), 300s and 900s, and 3600s ("scrub
    # the whole retention window") cadences on top of the recent/aged pair,
    # which is otherwise unchanged.
    derived_intervals_s: list[float] = Field(
        default_factory=lambda: [60.0, 300.0, 900.0, 3600.0]
    )
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
    # live-edge pass that had grown to ~65s (it fed several tiers from the
    # same decode at the time), so the effective cadence was ~100s and
    # measured lag ~105s -- past the ~90s
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
    # Wall-clock reserved out of `backfill_time_budget_s`, exclusively for the
    # derived-tier decimation pass (generate_derived, run last each cycle).
    # Without this, backfill's own demand doesn't reliably hit zero -- measured
    # on this deployment, two or three cameras have a persistent small trickle
    # of real holes every cycle (motion-driven recording gaps), so backfill
    # alone consumes the whole shared deadline and decimation never runs at
    # all: traced directly, backfill burned 22s on 4 of 10 cameras and
    # derived-tier generation got exactly zero cycles across several minutes of
    # live operation. This carves out a floor for it regardless of how hungry
    # backfill is; backfill still gets everything left over. Set to 0 to
    # restore the old "decimation gets pure leftovers" behaviour.
    derive_time_reserve_s: float = 5.0

    @field_validator("format")
    @classmethod
    def _known_format(cls, v: str) -> str:
        fmt = v.strip().lower()
        if fmt not in ("jpeg", "webp"):
            raise ValueError(f"scrub.format must be 'jpeg' or 'webp', got {v!r}")
        return fmt

    @field_validator("generate_interval_s", "live_edge_interval_s", "sheet_version_grace_s")
    @classmethod
    def _positive(cls, v: float) -> float:
        """A non-positive tick would spin the generation loop without yielding,
        and a non-positive grace would sweep a version the index is advertising
        in the same breath it publishes it.

        `generate_interval_s` is checked too because it is now the ceiling on the
        loop's tick (`min` of the two), so a zero there defeats the check on
        `live_edge_interval_s` entirely.
        """
        if v <= 0:
            raise ValueError(f"must be > 0, got {v!r}")
        return v

    @field_validator("derive_time_reserve_s")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        """0 is a valid, explicit "no floor" setting -- unlike the tick
        constants above, there's nothing broken about turning this off."""
        if v < 0:
            raise ValueError(f"must be >= 0, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_derived_intervals(self) -> ScrubSection:
        """Every entry in `derived_intervals_s` must be strictly coarser than
        `aged_interval_s` (otherwise it isn't a derived tier, just a duplicate
        of the aged decode tier), distinct from every other entry (otherwise
        two tiers would generate and serve identical buckets under the same
        interval, silently clobbering each other), and land on the same
        epoch-anchored grid every interval uses (`grid.decimate_to_grid`,
        `grid.grid_point`): bucket/slot boundaries are `k * interval` from
        absolute epoch zero, so an interval that isn't a whole multiple of
        `aged_interval_s` puts that tier's grid points out of step with the
        aged tier's at every boundary but the first.

        This is a static, config-time check against the coarser decode tier
        only. It can't see that `match_keyframe_cadence` may raise a given
        camera's *recent* tier past its configured value -- the generator
        checks that at runtime per camera before decimating from it
        (`generator._is_whole_multiple`).
        """
        seen: set[float] = set()
        for derived in self.derived_intervals_s:
            if derived in seen:
                raise ValueError(
                    f"scrub.derived_intervals_s must not repeat a value (got {derived!r} twice)"
                )
            seen.add(derived)
            if derived <= self.aged_interval_s:
                raise ValueError(
                    "scrub.derived_intervals_s entries must each be > scrub.aged_interval_s "
                    f"(got derived={derived!r}, aged={self.aged_interval_s!r})"
                )
            ratio = derived / self.aged_interval_s
            if abs(ratio - round(ratio)) > 1e-6:
                raise ValueError(
                    "scrub.derived_intervals_s entries must each be a whole multiple of "
                    f"scrub.aged_interval_s to land on its epoch grid "
                    f"(got derived={derived!r}, aged={self.aged_interval_s!r})"
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
    # Dwell input only -- `frigate/reviews` stays the sole authority on
    # whether anything is push-worthy. See `dwell_source` below.
    mqtt_topic_events: str = "frigate/events"
    # -- MQTT flight recorder --
    # Rolling capture of every consumed reviews/events message, so any real
    # situation can be replayed exactly (tools/replay_capture.py) instead of
    # approximated by a hand-written scenario. JSONL, size-rotated (one .1
    # sibling kept). Empty path -> "mqtt-capture.jsonl" next to
    # push_settings_path.
    capture_enabled: bool = True
    capture_path: str = ""
    capture_max_bytes: int = 64 * 1024 * 1024

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

    # -- Situations (notification-experience plan §8) --
    # Situation handles carry a pre-warmed thumbnail and outlive the v1 ones:
    # plan §8 retains them for 24h so a notification the user comes back to
    # hours later can still redeem its image.
    situation_handle_ttl_s: float = 86400.0
    # Plan §6: max N pushes per situation per device per hour (rolling).
    # Protects against a runaway camera, which is a different problem from
    # snooze -- that one protects against the user's own choice.
    rate_limit_per_hour: int = 10
    rate_limit_window_s: float = 3600.0
    # Pre-warmed thumbnail (plan §4 lever 1). ~320px/q60 lands around 10-20KB;
    # the NSE runs under a very tight memory ceiling and the phone may be on a
    # cold radio, so bigger buys nothing a notification can show.
    thumbnail_max_edge: int = 320
    thumbnail_quality: int = 60
    thumbnail_timeout_s: float = 5.0
    # Where a situation's loiter check gets its clock and its zone occupancy.
    #
    # "events" subscribes to `frigate/events` for dwell only. "reviews" is the
    # handoff's literal prescription -- dwell advanced solely by
    # `frigate/reviews` `type: update` messages. Measured on this deployment
    # (19.6 min, 2026-08-05) that topic published two review items as a `new`
    # and an `end` 30s apart with no update in between, because Frigate
    # publishes a review update when the item's *data* changes and a person
    # standing still changes nothing. A loiter threshold fed only from there
    # is never re-evaluated and never fires; "events" is the default for that
    # reason. Neither setting lets the object stream trigger a push on its own.
    dwell_source: str = "events"

    # -- Live Activities (Phase 2) --
    # Coalescing floor for update pushes -- one per activity per this many
    # seconds however busy the object stream gets. iOS meters LA updates.
    activity_update_min_interval_s: float = 3.0
    # Quiet period after which a Present situation counts as resolved. The
    # faster signal is Frigate's own object `end`, which the engine acts on
    # directly; this catches the case where it never arrives.
    activity_resolution_s: float = 30.0
    # How long the activity lingers on screen after the end push.
    activity_dismissal_tail_s: float = 30.0
    # Separate from the alert tier's `rate_limit_per_hour`, in both
    # directions: a chatty activity must not eat the budget a genuine
    # interrupt needs, and a silent update is nothing like a buzz.
    activity_updates_per_hour: int = 60
    activity_reap_after_s: float = 300.0
    # How often the resolution sweeper runs. Only ever *ends* activities, so
    # it is not the clock-driven keep-alive the plan forbids.
    activity_sweep_interval_s: float = 5.0

    # -- Attention ladder: delivery pipeline (Elsinore Phase 2) --
    # Off by default, independent of `enabled` -- this wraps the ladder
    # evaluator with card state and ordinary alert/silent pushes; it ships
    # dark until the wire-up's subject/place classification (currently an
    # MVP heuristic off `frigate/reviews` labels and `delivery_zone_place_map`)
    # is trusted against a live deployment. See docs/push-notifications.md.
    delivery_enabled: bool = True
    # Superseded by the user-editable `settings.zone_classes` (Elsinore
    # Phase 4, `push/policy_settings.py`) -- `delivery_wire.classify_place`
    # no longer reads this field. Left in place (unread) rather than
    # removed, since it's still a valid YAML key an existing deployment's
    # config file may set.
    delivery_zone_place_map: dict[str, str] = Field(default_factory=dict)
    # Design doc §3: an unhandled `urgent` card may re-alert once, this long
    # after its last sound.
    delivery_urgent_resound_s: float = 120.0
    delivery_urgent_resound_enabled: bool = True
    delivery_urgent_resound_max: int = 5
    # How often the urgent re-sound sweep runs. Only ever emits the one
    # re-sound a card is owed -- not a keep-alive.
    delivery_resound_sweep_interval_s: float = 15.0
    # Backfilled events older than this are discarded rather than replayed.
    delivery_backfill_staleness_s: float = 300.0
    # Live Activity stale-date offset from now.
    delivery_la_stale_s: float = 900.0
    # Relay auth key — sent as x-relay-key header on every relay request.
    relay_key: str = ""
    # Phone-reachable base URL for *this sidecar instance*, e.g.
    # "http://192.168.50.207:5001" or "https://sidecar.example.com". Used to
    # build the complete URL the card contract's `media` field documents
    # (docs/apns-payload-spec.md) -- unlike the v1/situations flow, which
    # only ever sends `handle` + `server_id` and lets the already-registered
    # app resolve the base URL itself, the card contract is a single
    # self-authorizing URL, so the sidecar has to know its own externally
    # reachable address to build it. Never Frigate's address -- Frigate is
    # never exposed to the phone directly; the sidecar fetches the snapshot
    # itself (`frigate.base_url`, LAN-internal) and re-hosts it behind a
    # minted handle at `/v1/push/thumbnail/{handle}`, same as situations.
    # Empty (the default) omits `media` entirely -- no broken link.
    external_base_url: str = ""

    # -- Live Activities for cards (Elsinore Phase 3) --
    # Master switch, independent of `delivery_enabled` (which must also be
    # on -- a card LA is an additional output channel for the same card
    # lifecycle, never a substitute for it). Off doesn't undo `delivery_la_families`;
    # it's the fast, whole-feature kill switch.
    delivery_la_enabled: bool = True
    # Superseded by the user-editable `settings.live_activities` (Elsinore
    # Phase 4, `push/policy_settings.py`) -- `delivery_wire.py` no longer
    # reads this field for per-family gating. Left in place (unread) for
    # the same reason as `delivery_zone_place_map` above.
    delivery_la_families: dict[str, bool] = Field(default_factory=dict)

    # -- Attention ladder settings API (Elsinore Phase 4) --
    # Where the user-editable policy document (routing table, zone-class
    # assignments, LA family toggles) is persisted. JSON, not YAML -- the
    # app PUTs a JSON body and round-tripping it through YAML's type
    # coercion on the way back out is a bug factory, not a feature. Created
    # with defaults on first read if it doesn't exist yet
    # (`push/policy_settings.py`).
    push_settings_path: str = "config/push_settings.json"

    # Where the uploaded floorplan/site image behind the /cameras layout map
    # is stored (extension appended per upload type). Same runtime-data class
    # as push_settings_path, so it lives next to it.
    floorplan_path: str = "config/floorplan"

    @field_validator("dwell_source")
    @classmethod
    def _known_dwell_source(cls, v: str) -> str:
        s = v.strip().lower()
        if s not in ("events", "reviews"):
            raise ValueError(f"push.dwell_source must be 'events' or 'reviews', got {v!r}")
        return s

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
