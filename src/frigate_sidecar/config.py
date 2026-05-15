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
