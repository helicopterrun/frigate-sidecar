from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar.config import (
    FrigateSection,
    Settings,
    SidecarSection,
)
from frigate_sidecar.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    # Frigate config.yml is optional; sidecar should handle a missing file.
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False
        ),
    )
    return TestClient(create_app(settings))


def test_list_html(client: TestClient) -> None:
    r = client.get("/triage")
    assert r.status_code == 200
    assert "frigate-sidecar" in r.text
    # Each fixture camera should appear in the camera filter.
    assert "alley-overview" in r.text
    assert "street-overview" in r.text


def test_list_filter_by_camera(client: TestClient) -> None:
    r = client.get("/triage", params={"camera": "alley-east", "days": 7})
    assert r.status_code == 200
    # `alley-east` is in the fixture but only as a dog event; should render.
    assert "alley-east" in r.text


def test_detail_html(client: TestClient) -> None:
    r = client.get("/event/e1")
    assert r.status_code == 200
    assert "alley-overview" in r.text
    # Detail page exposes label buttons.
    assert 'data-label="tp"' in r.text


def test_detail_404(client: TestClient) -> None:
    r = client.get("/event/does-not-exist")
    assert r.status_code == 404


def test_label_round_trip(client: TestClient) -> None:
    r = client.post(
        "/label",
        json={"event_id": "e1", "label": "fp", "note": "porch shadow"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Default `submit_plus=False` means no Plus call was attempted.
    assert body["plus"] == {"status": "not_requested"}
    # The list view filtered to fp should now include e1.
    r2 = client.get("/triage", params={"triage": "fp"})
    assert "e1" in r2.text


def test_label_with_plus_disabled_skips(client: TestClient) -> None:
    """If submit_plus=True but the app's plus_enabled is False, we return
    a `skipped/plus_not_enabled` status without making any HTTP calls."""
    r = client.post(
        "/label",
        json={"event_id": "e1", "label": "fp", "submit_plus": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["plus"]["status"] == "skipped"
    assert body["plus"]["reason"] == "plus_not_enabled"


def test_label_invalid(client: TestClient) -> None:
    r = client.post("/label", json={"event_id": "e1", "label": "garbage"})
    assert r.status_code == 400


def test_clear_label(client: TestClient) -> None:
    client.post("/label", json={"event_id": "e1", "label": "fp"})
    r = client.post("/clear-label", json={"event_id": "e1"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_degraded_when_mqtt_disconnected(client: TestClient) -> None:
    """A dead subscriber must surface as 503 -- the 2026-08-11 outage sat
    invisible for 41 hours behind a static-ok healthcheck."""

    class _DeadSubscriber:
        connected = False

    client.app.state.settings.push.enabled = True
    client.app.state.push_subscriber = _DeadSubscriber()
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["checks"]["mqtt"] == "disconnected"


def test_motion_blank_form(client: TestClient) -> None:
    # No baseline/target: page renders the form + a "set a target" empty state.
    r = client.get("/motion")
    assert r.status_code == 200
    assert "frigate-sidecar" in r.text
    # Header nav renders both pages and motion is the active link.
    assert 'class="page-link active"' in r.text
    assert "Motion" in r.text
    assert "Triage" in r.text


def test_motion_error_on_unreachable_frigate(client: TestClient) -> None:
    # Live API not reachable -> error banner, but page still 200.
    r = client.get("/motion", params={"baseline": "yesterday", "target": "today"})
    assert r.status_code == 200
    assert "Frigate API unreachable" in r.text or "date parse error" in r.text


def test_list_has_active_nav(client: TestClient) -> None:
    r = client.get("/triage")
    assert r.status_code == 200
    # Verify the nav with active=triage shows up.
    assert "page-link active" in r.text


def test_score_histogram_page(client: TestClient) -> None:
    r = client.get("/score-histogram", params={"days": 7})
    assert r.status_code == 200
    # Filter form is present + nav highlights scores.
    assert "Score Histogram" in r.text or "score-histogram" in r.text
    assert ">Scores<" in r.text
    # The fixture has events for alley-overview, etc.
    assert "alley-overview" in r.text


def test_score_histogram_camera_filter(client: TestClient) -> None:
    r = client.get(
        "/score-histogram",
        params={"days": 7, "camera": "alley-east"},
    )
    assert r.status_code == 200
    # Selected option markup present.
    assert 'value="alley-east" selected' in r.text


def test_fps_budget_page_error_when_api_down(client: TestClient) -> None:
    # Test config points at a non-existent Frigate; page should still 200
    # with an error banner.
    r = client.get("/fps-budget")
    assert r.status_code == 200
    assert "Frigate API unreachable" in r.text


def test_nav_appears_on_all_pages(client: TestClient) -> None:
    # Same nav on all pages.
    for path in ("/", "/motion", "/score-histogram", "/fps-budget"):
        r = client.get(path)
        assert r.status_code == 200, path
        for label in ("Triage", "Motion", "Scores", "FPS budget"):
            assert label in r.text, f"{label} missing on {path}"


# ----- Analysis HTTP routes -----


def test_analysis_motion_rate(client: TestClient) -> None:
    r = client.get("/analysis/motion-rate", params={"days": 7})
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    assert any(row["camera"] == "alley-overview" for row in rows)


def test_analysis_score_histogram(client: TestClient) -> None:
    r = client.get("/analysis/score-histogram", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body and "buckets" in body


def test_analysis_zone_hits(client: TestClient) -> None:
    r = client.get("/analysis/zone-hits", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert "hits" in body and "mask_candidates" in body


def test_analysis_pull_events(client: TestClient) -> None:
    r = client.get("/analysis/pull-events", params={"days": 7, "limit": 100})
    assert r.status_code == 200
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert ids == {"e1", "e2", "e3", "e4"}


def test_analysis_pull_events_camera_filter(client: TestClient) -> None:
    r = client.get(
        "/analysis/pull-events", params={"days": 7, "camera": "alley-overview"}
    )
    assert r.status_code == 200
    rows = r.json()
    assert {row["camera"] for row in rows} == {"alley-overview"}


def test_analysis_fps_budget_502_on_api_failure(client: TestClient) -> None:
    # No real Frigate API at the configured base URL in this test -> 502.
    r = client.get("/analysis/fps-budget")
    assert r.status_code == 502


def test_alignment_state_empty(client: TestClient) -> None:
    r = client.get("/analysis/annotation-offset/state")
    assert r.status_code == 200
    body = r.json()
    assert body["running"] is False
    assert body["results"] is None
    assert body["applied_ms"] == {}


def test_alignment_apply_and_state_roundtrip(client: TestClient) -> None:
    r = client.post(
        "/analysis/annotation-offset/apply",
        json={"offsets": {"doorbell": -5000, "gate": 250}},
    )
    assert r.status_code == 200
    assert r.json()["applied"] == {"doorbell": -5000, "gate": 250}

    body = client.get("/analysis/annotation-offset/state").json()
    assert body["applied_ms"] == {"doorbell": -5000, "gate": 250}
    # Config has no annotation_offset for these cameras.
    assert body["config_ms"]["doorbell"] == 0

    # Zero clears the override.
    r = client.post("/analysis/annotation-offset/apply", json={"offsets": {"gate": 0}})
    assert r.status_code == 200
    body = client.get("/analysis/annotation-offset/state").json()
    assert body["applied_ms"] == {"doorbell": -5000}


def test_alignment_apply_rejects_garbage(client: TestClient) -> None:
    assert client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {}}
    ).status_code == 422
    assert client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {"cam": "soon"}}
    ).status_code == 422
    assert client.post(
        "/analysis/annotation-offset/apply", json={"offsets": {"cam": 120_000}}
    ).status_code == 422


def test_alignment_measure_runs_in_background(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frigate_sidecar.analysis import annotation_offset as ao_mod

    def _fake_analyze(**kwargs: object) -> list[dict[str, object]]:
        assert kwargs["search_window_ms"] == 8000  # wider than the CLI default
        return [{"camera": "doorbell", "suggested_offset_ms": -4950,
                 "median_offset_ms": -4980, "iqr_ms": 120, "confidence": "high",
                 "n_qualifying_events": 12, "n_contributing_events": 11}]

    monkeypatch.setattr(ao_mod, "analyze", _fake_analyze)
    r = client.post("/analysis/annotation-offset/measure", params={"days": 3})
    assert r.status_code == 200
    # TestClient runs the loop to completion between requests, so the
    # background task has finished by the next call.
    body = client.get("/analysis/annotation-offset/state").json()
    assert body["running"] is False
    assert body["results"][0]["camera"] == "doorbell"
    assert body["measured_at"] is not None


def test_alignment_state_lists_frigate_cameras(client: TestClient) -> None:
    body = client.get("/analysis/annotation-offset/state").json()
    # All fixture cameras, sorted, even with no measurement or applied offset.
    assert body["cameras"] == ["alley-east", "alley-overview", "street-overview"]
    # config_ms covers them too (fixture config has no offsets -> 0).
    assert body["config_ms"]["alley-east"] == 0


def test_alignment_events_lists_recent_per_camera(client: TestClient) -> None:
    r = client.get(
        "/analysis/annotation-offset/events", params={"camera": "alley-overview"}
    )
    assert r.status_code == 200
    events = r.json()
    # e1 (-300 s), e2 (-600 s), e5 (-30 d) — newest first, other cameras absent.
    assert [e["id"] for e in events] == ["e1", "e2", "e5"]
    assert events[0]["label"] == "person"
    assert events[0]["end_time"] is not None

    r = client.get(
        "/analysis/annotation-offset/events",
        params={"camera": "alley-overview", "limit": 1},
    )
    assert [e["id"] for e in r.json()] == ["e1"]

    r = client.get(
        "/analysis/annotation-offset/events", params={"camera": "no-such-camera"}
    )
    assert r.json() == []


def test_alignment_frame_proxies_recording_snapshot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from frigate_sidecar import frigate_api

    calls: list[tuple[str, float]] = []

    def _fake_snapshot(
        self: object, camera: str, ts: float, *, timeout: float = 15.0
    ) -> tuple[bytes | None, int]:
        calls.append((camera, ts))
        if camera == "gone":
            return (None, 404)
        return (b"\xff\xd8jpeg", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "recording_snapshot", _fake_snapshot)
    ts = time.time() - 300
    r = client.get("/analysis/annotation-offset/frame/doorbell", params={"ts": ts})
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in r.headers["cache-control"]
    assert calls[0][0] == "doorbell"

    r = client.get("/analysis/annotation-offset/frame/gone", params={"ts": ts})
    assert r.status_code == 404


def test_alignment_thumbnail_proxies_frigate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Served by the sidecar, not the browser proxy: Frigate's nginx 401s
    proxied /api/events image requests when Frigate auth is on (the calibrator
    shipped with broken thumbnails because of exactly that)."""
    from frigate_sidecar import frigate_api

    def _fake_thumbnail(
        self: object, event_id: str, *, timeout: float = 10.0
    ) -> tuple[bytes | None, int]:
        if event_id == "gone":
            return (None, 404)
        return (b"\xff\xd8thumb", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "event_thumbnail", _fake_thumbnail)
    r = client.get("/analysis/annotation-offset/thumbnail/ev1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8thumb"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in r.headers["cache-control"]

    r = client.get("/analysis/annotation-offset/thumbnail/gone")
    assert r.status_code == 404


def test_alignment_snapshot_proxies_frigate(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calibrator's reference pane: full frame with the bbox drawn, via the
    sidecar's authorized connection (the proxy path 401s, same as thumbnails)."""
    from frigate_sidecar import frigate_api

    def _fake_snapshot(
        self: object, event_id: str, *, height: int = 480, bbox: bool = True,
        timeout: float = 10.0,
    ) -> tuple[bytes | None, int]:
        if event_id == "gone":
            return (None, 404)
        return (b"\xff\xd8snap", 200)

    monkeypatch.setattr(frigate_api.FrigateClient, "event_snapshot_jpeg", _fake_snapshot)
    r = client.get("/analysis/annotation-offset/snapshot/ev1")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8snap"
    assert r.headers["content-type"] == "image/jpeg"
    assert "max-age=3600" in r.headers["cache-control"]

    r = client.get("/analysis/annotation-offset/snapshot/gone")
    assert r.status_code == 404


def test_alignment_frame_rejects_bad_ts(client: TestClient) -> None:
    import time

    for bad in (0, -5, time.time() + 3600, time.time() - 45 * 86400):
        r = client.get("/analysis/annotation-offset/frame/cam", params={"ts": bad})
        assert r.status_code == 422, bad
