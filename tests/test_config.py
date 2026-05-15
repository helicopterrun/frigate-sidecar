from __future__ import annotations

from pathlib import Path

import pytest

from frigate_sidecar.config import Settings, load_settings


def test_defaults_when_no_file_no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(__import__("os").environ):
        if k.startswith("FRIGATE_SIDECAR_"):
            monkeypatch.delenv(k, raising=False)
    s = load_settings(config_path="/nonexistent/path.yml")
    assert s.sidecar.bind_port == 5001
    assert s.frigate.base_url.startswith("http://")


def test_yaml_overrides_defaults(tmp_path: Path) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text(
        """
frigate:
  base_url: http://example.test:5000
sidecar:
  bind_port: 9999
log_level: DEBUG
"""
    )
    s = load_settings(config_path=cfg)
    assert s.frigate.base_url == "http://example.test:5000"
    assert s.sidecar.bind_port == 9999
    assert s.log_level == "DEBUG"


def test_env_overrides_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "sidecar.yml"
    cfg.write_text("sidecar:\n  bind_port: 9999\n")
    monkeypatch.setenv("FRIGATE_SIDECAR_SIDECAR__BIND_PORT", "1234")
    s = load_settings(config_path=cfg)
    assert s.sidecar.bind_port == 1234


def test_settings_constructible_directly() -> None:
    s = Settings()
    assert s.sidecar.bind_port == 5001
