"""Tests for the transparent Frigate reverse proxy (routes/proxy.py).

Covers docs/scrub-cache-and-proxy-spec.md §6: Range/Authorization forwarding,
206/401/404 mirroring, traversal rejection, set-cookie/etag relay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from frigate_sidecar.config import FrigateSection, ProxySection, Settings, SidecarSection
from frigate_sidecar.server import create_app


class _StubResponse:
    def __init__(self, status_code: int, headers: dict[str, str], body: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    async def aiter_bytes(self) -> Any:
        yield self._body

    async def aclose(self) -> None:
        return None


class _StubAsyncClient:
    """Stand-in for httpx.AsyncClient that records the outgoing request and
    returns a canned response, so tests don't hit the network."""

    last_request: dict[str, Any] = {}
    next_response: _StubResponse | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def build_request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        req = {"method": method, "url": url, **kwargs}
        _StubAsyncClient.last_request = req
        return req

    async def send(self, req: dict[str, Any], stream: bool = False) -> _StubResponse:
        assert _StubAsyncClient.next_response is not None
        return _StubAsyncClient.next_response

    async def aclose(self) -> None:
        return None


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            proxy_base_url="http://frigate.test:8971",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        proxy=ProxySection(enabled=True),
    )
    return TestClient(create_app(settings))


@pytest.fixture(autouse=True)
def _stub_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", _StubAsyncClient)


def test_forwards_range_and_authorization_and_mirrors_206(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        206,
        {"content-type": "video/mp4", "content-range": "bytes 0-99/200", "accept-ranges": "bytes"},
        b"chunk",
    )
    r = client.get(
        "/vod/doorbell/index.m3u8",
        headers={"Range": "bytes=0-99", "Authorization": "Bearer abc"},
    )
    assert r.status_code == 206
    assert r.headers["content-range"] == "bytes 0-99/200"
    fwd = {k.lower(): v for k, v in _StubAsyncClient.last_request["headers"].items()}
    assert fwd["range"] == "bytes=0-99"
    assert fwd["authorization"] == "Bearer abc"


def test_mirrors_401_and_relays_www_authenticate(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        401, {"www-authenticate": 'Basic realm="frigate"'}, b""
    )
    r = client.get("/api/config")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Basic realm="frigate"'


def test_mirrors_404(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(404, {}, b"")
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404


def test_relays_set_cookie_and_etag(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(
        200, {"set-cookie": "session=xyz; Path=/", "etag": '"abc123"'}, b"{}"
    )
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.headers["set-cookie"] == "session=xyz; Path=/"
    assert r.headers["etag"] == '"abc123"'


def test_forwards_cookie_header(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(200, {"content-type": "application/json"}, b"{}")
    r = client.get("/api/config", headers={"Cookie": "session=abc"})
    assert r.status_code == 200
    fwd = {k.lower(): v for k, v in _StubAsyncClient.last_request["headers"].items()}
    assert fwd["cookie"] == "session=abc"


def test_forwards_method_pass_through_for_post(client: TestClient) -> None:
    _StubAsyncClient.next_response = _StubResponse(200, {"content-type": "application/json"}, b"{}")
    r = client.post("/api/reviews/viewed", json={"ids": ["e1"]})
    assert r.status_code == 200
    assert _StubAsyncClient.last_request["method"] == "POST"


def test_traversal_rejected(client: TestClient) -> None:
    _StubAsyncClient.next_response = None  # if the guard is bypassed, .send() will AssertionError
    # %2e%2e survives client-side URL normalization (unlike a literal "..",
    # which httpx collapses before the request ever leaves the test client),
    # so this actually exercises the proxy's own traversal guard.
    r = client.get("/api/%2e%2e/%2e%2e/etc/passwd", follow_redirects=False)
    assert r.status_code == 400


def test_proxy_disabled_returns_404(
    frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path
) -> None:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(config_path=fake_config, db_path=frigate_db_path),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
        proxy=ProxySection(enabled=False),
    )
    c = TestClient(create_app(settings))
    r = c.get("/api/config")
    assert r.status_code == 404
