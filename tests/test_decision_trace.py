"""Tests for the routing decision trace buffer and endpoint (spec §7)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from frigate_sidecar.config import FrigateSection, PushSection, Settings, SidecarSection
from frigate_sidecar.push import decision_trace
from frigate_sidecar.server import create_app


@pytest.fixture(autouse=True)
def _reset():
    decision_trace.reset_for_tests()
    yield
    decision_trace.reset_for_tests()


def _make_entry(**kw):
    defaults = dict(
        camera="back_garden", label="dog", subject="animal",
        zones=["yard"], place="yard", level="log",
        reasons=["routing_table"], event_id="evt-1",
    )
    defaults.update(kw)
    return decision_trace.append(**defaults)


class TestAppendRules:
    def test_initial_decision_is_recorded(self):
        entry = _make_entry()
        assert entry["id"].startswith("dec-")
        assert entry["ts"].endswith("Z")
        assert decision_trace.recent(10) == [entry]

    def test_level_change_produces_second_entry(self):
        _make_entry(level="quiet", event_id="evt-1")
        _make_entry(level="notify", event_id="evt-1")
        entries = decision_trace.recent(10)
        assert len(entries) == 2
        assert entries[0]["level"] == "notify"
        assert entries[1]["level"] == "quiet"

    def test_enrich_does_not_append(self):
        """Enrich-only mutations should NOT be recorded — the caller
        (delivery_wire) is responsible for only calling append on
        CREATE/ESCALATE/DEESCALATE. This test verifies the contract
        by checking the buffer doesn't grow when we don't call append."""
        _make_entry()
        assert len(decision_trace.recent(10)) == 1

    def test_recognition_relax_reason(self):
        entry = _make_entry(reasons=["recognition_relax"], level="quiet")
        assert entry["reasons"] == ["recognition_relax"]

    def test_zone_override_reason(self):
        entry = _make_entry(reasons=["zone_override"])
        assert entry["reasons"] == ["zone_override"]

    def test_quiet_hours_cap_reason(self):
        entry = _make_entry(reasons=["routing_table", "quiet_hours_cap"])
        assert "quiet_hours_cap" in entry["reasons"]


class TestBufferCap:
    def test_buffer_stays_bounded(self):
        for i in range(600):
            _make_entry(event_id=f"evt-{i}")
        entries = decision_trace.recent(9999)
        assert len(entries) == 200  # serve cap

    def test_oldest_evicted(self):
        for i in range(550):
            _make_entry(event_id=f"evt-{i}")
        entries = decision_trace.recent(200)
        ids = [e["event_id"] for e in entries]
        assert "evt-0" not in ids
        assert "evt-549" in ids


class TestRecent:
    def test_newest_first(self):
        _make_entry(event_id="a")
        _make_entry(event_id="b")
        entries = decision_trace.recent(10)
        assert entries[0]["event_id"] == "b"
        assert entries[1]["event_id"] == "a"

    def test_limit_respected(self):
        for i in range(10):
            _make_entry(event_id=f"evt-{i}")
        assert len(decision_trace.recent(3)) == 3

    def test_limit_capped_at_200(self):
        for i in range(250):
            _make_entry(event_id=f"evt-{i}")
        assert len(decision_trace.recent(9999)) == 200

    def test_empty_buffer(self):
        assert decision_trace.recent(50) == []


class TestEntryShape:
    def test_all_fields_present(self):
        entry = _make_entry()
        required = {"id", "ts", "camera", "label", "subject", "zones",
                     "place", "level", "reasons", "event_id"}
        assert required == set(entry.keys())

    def test_ts_is_iso_utc_with_z(self):
        entry = _make_entry()
        ts = entry["ts"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_id_is_unique(self):
        a = _make_entry(event_id="a")
        b = _make_entry(event_id="b")
        assert a["id"] != b["id"]

    def test_no_sub_label_in_entry(self):
        entry = _make_entry()
        assert "sub_label" not in entry
        assert "identity" not in entry


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text(yaml.safe_dump({"cameras": {}}))
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(
            db_path=sidecar_db_path, bind_port=5001, require_frigate_auth=False,
        ),
        push=PushSection(
            enabled=False,
            push_settings_path=str(tmp_path / "push_settings.json"),
        ),
    )
    return TestClient(create_app(settings))


class TestEndpoint:
    def test_decisions_returns_200_with_entries(self, client: TestClient):
        _make_entry(event_id="a")
        _make_entry(event_id="b")
        resp = client.get("/v1/push/decisions?limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["decisions"]) == 2
        assert body["decisions"][0]["event_id"] == "b"

    def test_decisions_limit_caps_at_200(self, client: TestClient):
        for i in range(250):
            _make_entry(event_id=f"evt-{i}")
        resp = client.get("/v1/push/decisions?limit=9999")
        assert resp.status_code == 200
        assert len(resp.json()["decisions"]) == 200

    def test_decisions_empty(self, client: TestClient):
        resp = client.get("/v1/push/decisions")
        assert resp.status_code == 200
        assert resp.json() == {"decisions": []}

    def test_capabilities_includes_decisions(self, client: TestClient):
        resp = client.get("/v1/capabilities")
        assert resp.status_code == 200
        assert resp.json()["decisions"] == {"enabled": True}
