"""Golden contract fixtures vendored by the iOS app (Elsinore/FrigateKit).

Each file under `fixtures/contract/` is the exact JSON one of the live
builders or HTTP routes produces for a fixed, canned input -- deterministic
timestamps, fixed handles/ids/config, so the file on disk never changes
unless the wire contract does. The app repo copies this directory verbatim
(`Elsinore/tools/sync-contract-fixtures.sh`) and decodes every file with its
real Codable types, so a shape change here is a loud diff in both repos
instead of a phone that silently stops decoding.

`MANIFEST.json` carries a sha256 per fixture; both CIs verify it
independently (here: `test_contract_manifest_matches_files`; app: a Swift
test), so hand-edits/corruption on either side fail loudly.

Set `CONTRACT_GOLDEN_REGEN=1` to rewrite the fixtures + MANIFEST.json after
reviewing that a contract change is intentional.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml
from fastapi.testclient import TestClient

from frigate_sidecar.config import (
    FrigateSection,
    PushSection,
    ScrubSection,
    Settings,
    SidecarSection,
)
from frigate_sidecar.push import decision_trace, live_activities, policy_settings
from frigate_sidecar.push.cards import CREATE, ESCALATE, RESOLVE, Card
from frigate_sidecar.push.delivery import build_card_payload
from frigate_sidecar.push.library import sound_file
from frigate_sidecar.server import create_app
from tests.conftest import FRIGATE_EVENT_SCHEMA
from tests.test_scrub import FULL_EVENT_SCHEMA, RECORDINGS_SCHEMA, REVIEWSEGMENT_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "contract"

#: Same key the APNS goldens carry, so LA fixtures double as `simctl push`
#: replay input.
SIMULATOR_TARGET_BUNDLE = "com.houseofpaimon.Elsinore"

#: Fixed inputs. Never `time.time()` -- a golden fixture has to be the same
#: file every run, not just the same shape.
SENT_AT = 1_785_952_622.704
HANDLE = "h_9f3a1c2d3e4f5061"
SERVER_ID = "s_a1b2c3"
CAMERA = "doorbell"
TRACK_ID = "t_1755550001-abc123"
CARD_KEY = "doorbell:package:t_1755550001-abc123"
EVENT_ID = "1755550001.123456-abc123"


@pytest.fixture(autouse=True)
def _isolated_globals() -> Iterator[None]:
    """These builders touch the process-wide active policy and the decision
    ring buffer; leave neither behind for other tests."""
    policy_settings.reset_for_tests()
    decision_trace.reset_for_tests()
    yield
    policy_settings.reset_for_tests()
    decision_trace.reset_for_tests()


# ---------------------------------------------------------------------------
# TestClient plumbing (mirrors tests/test_push_settings_routes.py)
# ---------------------------------------------------------------------------


def _make_client(tmp: Path) -> TestClient:
    """A fresh app over a canned Frigate config + empty DBs, fully inside
    `tmp` so two consecutive builds share nothing."""
    frigate_db = tmp / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(FRIGATE_EVENT_SCHEMA)
    conn.commit()
    conn.close()

    fake_config = tmp / "frigate-config.yml"
    fake_config.write_text(
        yaml.safe_dump(
            {
                "cameras": {
                    "doorbell": {
                        "zones": {"front_door": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    },
                    "street": {
                        "zones": {
                            "nw_49th_st": {"coordinates": "0,0,1,0,1,1,0,1"},
                            "sidewalk": {"coordinates": "0,0,1,0,1,1,0,1"},
                        },
                    },
                    "backyard": {
                        "zones": {"garage": {"coordinates": "0,0,1,0,1,1,0,1"}},
                    },
                }
            }
        )
    )
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db,
        ),
        sidecar=SidecarSection(
            db_path=tmp / "frigate-sidecar.db", bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False, push_settings_path=str(tmp / "push_settings.json"),
        ),
    )
    return TestClient(create_app(settings))


# ---------------------------------------------------------------------------
# Canned builder inputs
# ---------------------------------------------------------------------------


def _content_state_full() -> dict[str, Any]:
    return live_activities.build_content_state(
        level="urgent",
        mutation=ESCALATE,
        glyph="shippingbox.fill",
        primary="Package at the front door",
        secondary="Courier left a box on the porch",
        elapsed_seconds=42,
        card_key=CARD_KEY,
        thumbnail_handle=HANDLE,
        thumbnail_revision=2,
        state_since_ts=SENT_AT - 42.0,
        story_started_ts=SENT_AT - 300.0,
        # Shapes as _build_motion / the zones-ladder builder in
        # delivery_wire.py actually emit them — the app's ContentState
        # requires motion.heading and zones.ladder/current_index.
        motion={"heading": "approaching", "speed_label": "walking"},
        zones={"ladder": ["Yard", "Front door"], "current_index": 1},
        path={"points": live_activities.downsample_path(
            [[0.1, 0.9], [0.2, 0.8], [0.35, 0.62], [0.5, 0.5], [0.62, 0.41]]
        )},
    )


def _content_state_minimal() -> dict[str, Any]:
    return live_activities.build_content_state(
        level="notify",
        mutation=CREATE,
        glyph="figure.walk",
        primary="Person at the doorbell",
        secondary="",
        elapsed_seconds=0,
        card_key=CARD_KEY,
        thumbnail_handle=None,
        thumbnail_revision=0,
    )


def _content_state_resolved() -> dict[str, Any]:
    return live_activities.build_content_state(
        level="urgent",
        mutation=RESOLVE,
        glyph="checkmark.circle.fill",
        primary="Package delivered",
        secondary="Box picked up from the porch",
        elapsed_seconds=95,
        card_key=CARD_KEY,
        thumbnail_handle=HANDLE,
        thumbnail_revision=3,
        state_since_ts=SENT_AT - 95.0,
    )


def _with_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "Simulator Target Bundle": SIMULATOR_TARGET_BUNDLE}


def _build_la_content_state() -> dict[str, Any]:
    return _content_state_full()


def _build_la_content_state_minimal() -> dict[str, Any]:
    return _content_state_minimal()


def _build_la_start() -> dict[str, Any]:
    return _with_bundle(live_activities.build_la_start_payload(
        content_state=_content_state_minimal(),
        family=live_activities.PACKAGE,
        camera=CAMERA,
        track_id=TRACK_ID,
        card_key=CARD_KEY,
        now=SENT_AT,
        sound=sound_file("package-delivery"),
    ))


def _build_la_update() -> dict[str, Any]:
    return _with_bundle(live_activities.build_la_update_payload(
        content_state=_content_state_full(), now=SENT_AT,
    ))


def _build_la_escalation() -> dict[str, Any]:
    return _with_bundle(live_activities.build_la_update_payload(
        content_state=_content_state_full(),
        now=SENT_AT,
        alert=True,
        alert_title="Package at the front door",
        alert_body="Still on the porch after 42s",
        sound=sound_file("package-delivery"),
        interruption_level="time-sensitive",
    ))


def _build_la_end() -> dict[str, Any]:
    return _with_bundle(live_activities.build_la_end_payload(
        content_state=_content_state_resolved(), now=SENT_AT,
    ))


def _canonical_put_body() -> dict[str, Any]:
    """The canonical PUT document: the full 7-subject one-alerts-stack shape
    with a handful of non-default cells so derivation is visible."""
    doc = policy_settings.default_settings()
    doc["zone_classes"]["front_door"] = "doors"
    doc["zone_classes"]["garage"] = "off_limits"
    doc["zone_overrides"] = {"sidewalk": {"person": "notify"}}
    doc["outcomes"]["package"]["doors"] = "notify"
    doc["outcomes"]["opening"]["yard"] = "off"
    doc["outcomes"]["thing"]["yard"] = "alarm"
    doc["camera_neighbors"] = {"doorbell": ["street"]}
    return doc


def _build_push_settings_put() -> dict[str, Any]:
    return _canonical_put_body()


def _build_push_settings_get() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        policy_settings.reset_for_tests()
        client = _make_client(Path(td))
        assert client.put("/v1/push/settings", json=_canonical_put_body()).status_code == 200
        resp = client.get("/v1/push/settings")
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        return body


def _build_push_decisions() -> dict[str, Any]:
    decision_trace.reset_for_tests()
    first = decision_trace.append(
        camera=CAMERA, label="package", subject="package",
        zones=["front_door"], place="doors", level="notify",
        reasons=["outcomes[package][doors]=notify"], event_id=EVENT_ID,
    )
    decision_trace.annotate(
        EVENT_ID, family=live_activities.PACKAGE, la_started=True, la_reason="family=package",
    )
    second = decision_trace.append(
        camera="street", label="car", subject="vehicle",
        zones=["nw_49th_st"], place="street", level="log",
        reasons=["outcomes[vehicle][street]=log"], event_id="1755550002.654321-def456",
    )
    decision_trace.annotate(
        "1755550002.654321-def456",
        family=live_activities.CATCH_ALL, la_started=False, la_reason="level=log",
    )
    # `append` stamps wall-clock `ts`; the entries returned are the buffered
    # objects, so pin the timestamps to fixed values for the golden file.
    first["ts"] = "2026-08-19T20:37:02Z"
    second["ts"] = "2026-08-19T20:37:41Z"
    with tempfile.TemporaryDirectory() as td:
        client = _make_client(Path(td))
        resp = client.get("/v1/push/decisions")
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        return body


def _build_capabilities() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        client = _make_client(Path(td))
        resp = client.get("/v1/capabilities")
        assert resp.status_code == 200
        body: dict[str, Any] = resp.json()
        return body


def _build_card_push() -> dict[str, Any]:
    card = Card(
        card_key=CARD_KEY,
        level="urgent",
        created_at=SENT_AT - 42.0,
        updated_at=SENT_AT,
        state_since_at=SENT_AT - 42.0,
        peak_level="urgent",
        sound_count=1,
        last_sound_at=SENT_AT - 42.0,
    )
    return build_card_payload(
        card,
        ESCALATE,
        sound=True,
        subject_kind="package",
        place_class="doors",
        label="package",
        camera=CAMERA,
        zone_name="front_door",
        glyph="shippingbox.fill",
        primary="Package at the front door",
        secondary="Courier left a box on the porch",
        event_ts=SENT_AT - 42.0,
        media=HANDLE,
        deep_link=f"elsinore://camera/{CAMERA}",
    )


# ---------------------------------------------------------------------------
# /v1 route goldens (Wave 6A-1) -- each builder spins up its own tiny
# TestClient over a canned Frigate/sidecar DB so the fixture is fully
# self-contained and never touches wall-clock time.
# ---------------------------------------------------------------------------


def _v1_client(tmp: Path, *, frigate_db: Path, sidecar_db: Path | None = None) -> TestClient:
    fake_config = tmp / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config, db_path=frigate_db,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db or (tmp / "frigate-sidecar.db"),
            bind_port=5001,
            require_frigate_auth=False,
        ),
        scrub=ScrubSection(enabled=False, retention_days=4, cache_dir=tmp / "scrub"),
        push=PushSection(enabled=False, push_settings_path=str(tmp / "push_settings.json")),
    )
    return TestClient(create_app(settings))


def _build_v1_coverage() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(RECORDINGS_SCHEMA)
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
            "segment_size) VALUES ('s0', 'doorbell', '/x/0.mp4', ?, ?, 10.0, 5.0)",
            (SENT_AT - 360, SENT_AT - 350),
        )
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
            "segment_size) VALUES ('s1', 'doorbell', '/x/1.mp4', ?, ?, 10.0, 5.0)",
            (SENT_AT - 350, SENT_AT - 340),
        )
        conn.commit()
        conn.close()
        client = _v1_client(tmp, frigate_db=frigate_db)
        with mock.patch("time.time", return_value=SENT_AT):
            resp = client.get(
                "/v1/coverage/doorbell", params={"start": SENT_AT - 400, "end": SENT_AT},
            )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


def _build_v1_scrub_sheets() -> dict[str, Any]:
    from frigate_sidecar import db as db_mod

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(RECORDINGS_SCHEMA)
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
            "segment_size) VALUES ('s0', 'doorbell', '/x/0.mp4', ?, ?, 10.0, 5.0)",
            (SENT_AT - 40, SENT_AT - 30),
        )
        conn.commit()
        conn.close()

        sidecar_db = tmp / "frigate-sidecar.db"
        sconn = db_mod.open_sidecar(sidecar_db)
        try:
            start = 1_785_380_400.0
            db_mod.upsert_scrub_sheet(
                sconn, camera="doorbell", start_ts=start, interval_s=1.0, cols=12, rows=8,
                cell_w=320, cell_h=180, count=24, path="doorbell/1.0/x.jpg", complete=False,
            )
            sconn.commit()
        finally:
            sconn.close()

        client = _v1_client(tmp, frigate_db=frigate_db, sidecar_db=sidecar_db)
        resp = client.get(
            "/v1/scrub/doorbell/sheets", params={"start": start, "end": start + 200},
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


def _build_v1_reel() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(RECORDINGS_SCHEMA)
        conn.executescript(FULL_EVENT_SCHEMA)
        conn.executescript(REVIEWSEGMENT_SCHEMA)
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
            "segment_size) VALUES ('s0', 'doorbell', '/x/0.mp4', ?, ?, 10.0, 5.0)",
            (SENT_AT - 200, SENT_AT - 190),
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot, data) VALUES ('e1', 'doorbell', 'package', ?, "
            "?, 0.91, '[\"front_door\"]', 'amazon', 1, 1, ?)",
            (SENT_AT - 150, SENT_AT - 140, json.dumps({"top_score": 0.91})),
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot) VALUES ('e2', 'doorbell', 'person', ?, NULL, "
            "0.5, '[]', NULL, 0, 1)",
            (SENT_AT - 50,),
        )
        conn.execute(
            "INSERT INTO reviewsegment (id, camera, start_time, end_time, severity, data) "
            "VALUES ('r1', 'doorbell', ?, ?, 'alert', ?)",
            (SENT_AT - 152, SENT_AT - 138,
             json.dumps({"objects": ["package"], "zones": ["front_door"], "detections": ["e1"]})),
        )
        conn.commit()
        conn.close()

        client = _v1_client(tmp, frigate_db=frigate_db)

        async def _fixed_motion(
            request: object, camera: str, start: float, end: float, scale: float
        ) -> tuple[list[float], bool]:
            n = int((end - start) / scale)
            return [0.0] * n, False

        from frigate_sidecar.routes import scrub as scrub_routes

        with (
            mock.patch("time.time", return_value=SENT_AT),
            mock.patch.object(scrub_routes, "_fetch_and_aggregate_motion", _fixed_motion),
        ):
            resp = client.get(
                "/v1/reel/doorbell",
                params={"start": SENT_AT - 300, "end": SENT_AT, "motion_scale": 10},
            )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


def _build_v1_highlights() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(RECORDINGS_SCHEMA)
        conn.executescript(FULL_EVENT_SCHEMA)
        conn.execute(
            "INSERT INTO recordings (id, camera, path, start_time, end_time, duration, "
            "segment_size) VALUES ('s0', 'doorbell', '/x/0.mp4', ?, ?, 10.0, 5.0)",
            (SENT_AT - 400, SENT_AT - 390),
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot, data) VALUES ('h0', 'doorbell', 'person', ?, "
            "?, NULL, '[]', NULL, 1, 1, ?)",
            (SENT_AT - 300, SENT_AT - 295, json.dumps({"top_score": 0.91})),
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot, data) VALUES ('h1', 'doorbell', 'car', ?, "
            "?, NULL, '[]', NULL, 1, 0, ?)",
            (SENT_AT - 200, SENT_AT - 195, json.dumps({"top_score": 0.62})),
        )
        conn.commit()
        conn.close()

        client = _v1_client(tmp, frigate_db=frigate_db)
        resp = client.get(
            "/v1/highlights/doorbell", params={"before": SENT_AT, "limit": 50},
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


def _build_v1_events_search() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(FULL_EVENT_SCHEMA)
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot, data) VALUES ('se1', 'doorbell', 'person', ?, "
            "?, NULL, '[\"front_door\"]', 'amazon', 1, 1, ?)",
            (SENT_AT - 300, SENT_AT - 295, json.dumps({"score": 0.91})),
        )
        conn.commit()
        conn.close()

        client = _v1_client(tmp, frigate_db=frigate_db)
        resp = client.get(
            "/v1/events/search", params={"cameras": "doorbell", "labels": "person", "limit": 10},
        )
        assert resp.status_code == 200, resp.text
        body: list[Any] = resp.json()
        return {"results": body}


def _build_v1_events_related() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(FULL_EVENT_SCHEMA)
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot, data) VALUES ('re1', 'doorbell', 'person', ?, "
            "?, NULL, '[]', NULL, 1, 1, ?)",
            (SENT_AT - 300, SENT_AT - 290, json.dumps({"score": 0.91})),
        )
        conn.execute(
            "INSERT INTO event (id, camera, label, start_time, end_time, top_score, zones, "
            "sub_label, has_clip, has_snapshot, data) VALUES ('re2', 'street', 'person', ?, "
            "?, NULL, '[]', NULL, 1, 1, ?)",
            (SENT_AT - 298, SENT_AT - 288, json.dumps({"score": 0.75})),
        )
        conn.commit()
        conn.close()

        client = _v1_client(tmp, frigate_db=frigate_db)
        resp = client.get("/v1/events/re1/related")
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


def _build_v1_push_map_live() -> dict[str, Any]:
    from frigate_sidecar.push.situations import TrackStore

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(FRIGATE_EVENT_SCHEMA)
        conn.commit()
        conn.close()

        client = _v1_client(tmp, frigate_db=frigate_db)
        policy_settings.reset_for_tests()
        active = dict(policy_settings.get_active())
        active.update({
            "camera_optics": {"doorbell": {"hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0}},
            "camera_layout": {"doorbell": {"x": 0.5, "y": 0.5, "azimuth": 0.0, "fov": 90.0}},
            "map_scale_ft": 200.0,
        })
        policy_settings.apply_settings(active)

        class _Engine:
            tracks = TrackStore()

        engine = _Engine()
        engine.tracks.observe_object(
            "doorbell", "t1", (), now=SENT_AT, path_data=((0.5, 0.7, SENT_AT),), label="person",
        )
        client.app.state.push_engine = engine

        with mock.patch("time.time", return_value=SENT_AT):
            resp = client.get("/v1/push/map/live", params={"debug": 1})
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


def _build_v1_push_map_track() -> dict[str, Any]:
    from frigate_sidecar.push.situations import TrackStore

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        frigate_db = tmp / "frigate.db"
        conn = sqlite3.connect(frigate_db)
        conn.executescript(FRIGATE_EVENT_SCHEMA)
        conn.commit()
        conn.close()

        client = _v1_client(tmp, frigate_db=frigate_db)
        policy_settings.reset_for_tests()
        active = dict(policy_settings.get_active())
        active.update({
            "camera_optics": {"doorbell": {"hfov": 90.0, "mount_ft": 10.0, "tilt_deg": 12.0}},
            "camera_layout": {"doorbell": {"x": 0.5, "y": 0.5, "azimuth": 0.0, "fov": 90.0}},
            "map_scale_ft": 200.0,
            "secure_area": {"x0": 0.4, "y0": 0.4, "x1": 0.6, "y1": 0.6},
        })
        policy_settings.apply_settings(active)

        class _Engine:
            tracks = TrackStore()

        engine = _Engine()
        path = tuple((0.5, 0.6 + 0.05 * i, SENT_AT - (2 - i)) for i in range(3))
        engine.tracks.observe_object(
            "doorbell", "ev1", (), now=SENT_AT, path_data=path, label="person",
        )
        client.app.state.push_engine = engine

        resp = client.get(
            "/v1/push/map/track", params={"camera": "doorbell", "event_id": "ev1"},
        )
        assert resp.status_code == 200, resp.text
        body: dict[str, Any] = resp.json()
        return body


#: fixture filename -> zero-arg builder. MANIFEST.json is derived, not built.
FIXTURE_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "la_content_state.json": _build_la_content_state,
    "la_content_state_minimal.json": _build_la_content_state_minimal,
    "la_start.json": _build_la_start,
    "la_update.json": _build_la_update,
    "la_escalation.json": _build_la_escalation,
    "la_end.json": _build_la_end,
    "push_settings_get.json": _build_push_settings_get,
    "push_settings_put.json": _build_push_settings_put,
    "push_decisions.json": _build_push_decisions,
    "capabilities.json": _build_capabilities,
    "card_push.json": _build_card_push,
    "v1_coverage.json": _build_v1_coverage,
    "v1_scrub_sheets.json": _build_v1_scrub_sheets,
    "v1_reel.json": _build_v1_reel,
    "v1_highlights.json": _build_v1_highlights,
    "v1_events_search.json": _build_v1_events_search,
    "v1_events_related.json": _build_v1_events_related,
    "v1_push_map_live.json": _build_v1_push_map_live,
    "v1_push_map_track.json": _build_v1_push_map_track,
}

MANIFEST_NAME = "MANIFEST.json"

#: GET-response fields the PUT body never carries: server-derived, read-only
#: wrapper keys (plus `rev`, which the server assigns on save).
SERVER_DERIVED_GET_FIELDS = (
    "rev",
    "available_cameras",
    "available_zones",
    "available_openings",
    "derived_headings",
    "placement_deployments",
    "recognition_available",
)


def _render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regen_requested() -> bool:
    return os.environ.get("CONTRACT_GOLDEN_REGEN", "") not in ("", "0", "false", "False")


@pytest.mark.parametrize("filename", sorted(FIXTURE_BUILDERS))
def test_contract_fixture_matches_builder(filename: str) -> None:
    builder = FIXTURE_BUILDERS[filename]
    path = FIXTURES_DIR / filename
    rendered = _render(builder())

    if _regen_requested() or not path.exists():
        # Determinism gate: nothing wall-clock may leak into a golden file.
        assert _render(builder()) == rendered, (
            f"{filename}: two consecutive builds differ -- a non-fixed input leaked in"
        )
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        return

    on_disk = path.read_text()
    if on_disk != rendered:
        pytest.fail(
            f"{path} no longer matches what the contract builders produce.\n\n"
            "If this change is intentional, review the diff below, re-bless with\n\n"
            f"    CONTRACT_GOLDEN_REGEN=1 pytest {Path(__file__).name}\n\n"
            "and re-run Elsinore/tools/sync-contract-fixtures.sh in the app repo.\n\n"
            f"--- fixture on disk ({path}) ---\n{on_disk}\n"
            f"--- current builder output ---\n{rendered}"
        )


def test_contract_manifest_matches_files() -> None:
    manifest_path = FIXTURES_DIR / MANIFEST_NAME
    current = {
        "version": 1,
        "files": {name: _sha256(FIXTURES_DIR / name) for name in sorted(FIXTURE_BUILDERS)},
    }

    if _regen_requested() or not manifest_path.exists():
        manifest_path.write_text(_render(current))
        return

    manifest = json.loads(manifest_path.read_text())
    assert manifest == current, (
        f"{manifest_path} out of date -- re-bless with CONTRACT_GOLDEN_REGEN=1"
    )
    # No orphan .json files sitting in the directory unaccounted for.
    on_disk = {p.name for p in FIXTURES_DIR.glob("*.json")} - {MANIFEST_NAME}
    assert on_disk == set(FIXTURE_BUILDERS)


def test_put_settings_roundtrip_matches_get_fixture(tmp_path: Path) -> None:
    """PUT the canonical put-fixture into a fresh server; GET must equal the
    stored get-fixture, modulo the explicitly server-derived wrapper fields
    (compared for presence, not value)."""
    put_doc = json.loads((FIXTURES_DIR / "push_settings_put.json").read_text())
    get_fixture = json.loads((FIXTURES_DIR / "push_settings_get.json").read_text())

    client = _make_client(tmp_path)
    assert client.put("/v1/push/settings", json=put_doc).status_code == 200
    live = client.get("/v1/push/settings").json()

    for field in SERVER_DERIVED_GET_FIELDS:
        assert field in live and field in get_fixture
    strip = set(SERVER_DERIVED_GET_FIELDS)
    live_cmp = {k: v for k, v in live.items() if k not in strip}
    fixture_cmp = {k: v for k, v in get_fixture.items() if k not in strip}
    assert live_cmp == fixture_cmp
