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
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_PATHS = (
    "/etc/frigate-sidecar/sidecar.yml",
    "./config/sidecar.yml",
)


class FrigateSection(BaseModel):
    base_url: str = "http://frigate.lan:5000"
    config_path: Path = Path("/opt/frigate/config.yml")
    db_path: Path = Path("/opt/frigate/database/frigate.db")


class SidecarSection(BaseModel):
    db_path: Path = Path("/data/frigate-sidecar.db")
    bind_host: str = "0.0.0.0"
    bind_port: int = 5001


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FRIGATE_SIDECAR_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    frigate: FrigateSection = Field(default_factory=FrigateSection)
    sidecar: SidecarSection = Field(default_factory=SidecarSection)
    log_level: str = "INFO"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings from YAML (if any) and merge with env-var overrides.

    `config_path` overrides everything; otherwise FRIGATE_SIDECAR_CONFIG env var,
    otherwise the first existing default search path.
    """
    chosen: Path | None = None
    if config_path:
        chosen = Path(config_path)
    elif env_path := os.environ.get("FRIGATE_SIDECAR_CONFIG"):
        chosen = Path(env_path)
    else:
        for candidate in DEFAULT_CONFIG_PATHS:
            p = Path(candidate)
            if p.exists():
                chosen = p
                break

    file_data: dict[str, Any] = _read_yaml(chosen) if chosen else {}
    # Build via Settings(**file_data) so env vars layer on top (BaseSettings
    # reads env at __init__).
    return Settings(**file_data)
