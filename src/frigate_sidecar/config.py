"""Sidecar configuration: YAML file + env-var overrides.

Loading precedence (highest wins):
1. Environment variables prefixed FRIGATE_SIDECAR_ (nested with __).
2. YAML file at FRIGATE_SIDECAR_CONFIG, or the default search path.
3. Defaults defined on the models below.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
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
    # Host-side path to Frigate's recordings root, as the sidecar itself sees it.
    # Deployment-specific — measured live at /mnt/frigate-storage/recordings/recordings
    # on the current box (nested `recordings/` segment; see docs spec §8.2 M6).
    recordings_path: Path = Path("/mnt/frigate-storage/recordings/recordings")


class SidecarSection(BaseModel):
    db_path: Path = Path("/data/frigate-sidecar.db")
    bind_host: str = "0.0.0.0"
    bind_port: int = 5001


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
    aged_after_h: float = 24.0
    retention_days: int = 4
    cell_w: int = 320
    cell_h: int = 180
    sheet_cols: int = 12
    sheet_rows: int = 8
    format: str = "jpeg"  # "jpeg" | "webp" -- JPEG measured smaller on real
    # camera content (see docs spec §5.3 M4); WebP requires -lossless 0.
    generate_interval_s: float = 60.0  # continuous edge, NOT hourly (§5.4)
    ffmpeg_concurrency: int = 3


class ProxySection(BaseModel):
    enabled: bool = True
    pass_request_headers: list[str] = Field(
        default_factory=lambda: ["range", "authorization", "cookie"]
    )


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
    log_level: str = "INFO"


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
