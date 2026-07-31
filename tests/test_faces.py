"""Tests for the face-curation path that reaches back into Frigate.

`POST /faces/decide` takes a library name straight from the request and used to
interpolate it into an upstream URL unescaped. httpx normalises `..` segments
and honours `?`, so the name controlled the whole upstream path and query --
an arbitrary-endpoint POST against Frigate's *unauthenticated* origin.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from frigate_sidecar.config import FaceSection, FrigateSection, Settings, SidecarSection
from frigate_sidecar.faces.promoter import FacePromoteError, promote
from frigate_sidecar.frigate_api import FrigateClient


def _client_with(handler: object) -> FrigateClient:
    fc = FrigateClient("http://frigate.test:5000")
    fc._client.close()
    fc._client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return fc


def test_train_face_percent_encodes_the_library_name() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "message": "ok"})

    fc = _client_with(handler)
    try:
        fc.train_face("../../../api/config?x=1&y=", "crop.webp")
    finally:
        fc.close()

    # Assert on the bytes that go on the wire: `.path` shows the decoded view,
    # but what Frigate's router sees is `raw_path`.
    raw = seen[0].url.raw_path.decode()
    assert raw.startswith("/api/faces/train/")
    assert raw.endswith("/classify")
    # The name stays one escaped path segment: no separator, no query.
    assert "/" not in raw[len("/api/faces/train/") : -len("/classify")]
    assert seen[0].url.query == b""


def test_train_face_keeps_ordinary_names_readable() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "message": "ok"})

    fc = _client_with(handler)
    try:
        fc.train_face("Christopher", "crop.webp")
    finally:
        fc.close()
    assert seen[0].url.path == "/api/faces/train/Christopher/classify"


@pytest.mark.parametrize(
    "name",
    ["../evil", "a/b", "a\\b", "..", "with\nnewline"],
)
def test_promote_rejects_path_shaped_names(tmp_path: Path, name: str) -> None:
    settings = Settings(
        frigate=FrigateSection(base_url="http://frigate.test:5000"),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db"),
        face=FaceSection(clips_faces_dir=tmp_path / "faces"),
    )
    with pytest.raises(FacePromoteError):
        promote(settings, "crop.webp", name)


def test_promote_rejects_path_shaped_filenames(tmp_path: Path) -> None:
    settings = Settings(
        frigate=FrigateSection(base_url="http://frigate.test:5000"),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db"),
        face=FaceSection(clips_faces_dir=tmp_path / "faces"),
    )
    with pytest.raises(FacePromoteError):
        promote(settings, "../../etc/passwd", "Christopher")
