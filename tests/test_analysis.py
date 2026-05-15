from __future__ import annotations

from pathlib import Path

from frigate_sidecar.analysis import (
    motion_rate,
    pull_events,
    score_histogram,
    zone_hits,
)
from frigate_sidecar.triage import recorder


def test_pull_events_emits_within_window(frigate_db_path: Path) -> None:
    rows = list(pull_events.pull(frigate_db=frigate_db_path, days=7))
    ids = {r["id"] for r in rows}
    assert ids == {"e1", "e2", "e3", "e4"}
    # e5 is 30 days old; absent from the 7-day window.
    assert "e5" not in ids


def test_pull_events_camera_filter(frigate_db_path: Path) -> None:
    rows = list(pull_events.pull(frigate_db=frigate_db_path, days=7, camera="alley-overview"))
    assert {r["camera"] for r in rows} == {"alley-overview"}


def test_motion_rate_smoke(frigate_db_path: Path) -> None:
    rows = motion_rate.analyze(frigate_db=frigate_db_path, days=7)
    # Each fixture camera that has events in the window appears.
    cams = {r["camera"] for r in rows}
    assert "alley-overview" in cams
    for r in rows:
        assert "suggestion" in r
        assert r["events_total"] >= 1


def test_score_histogram_sparse_then_with_triage(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    # No triage labels; fixture has <10 events per (camera,label) -> sparse rows.
    result = score_histogram.analyze(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        days=7,
    )
    assert all(r["confidence"] == "sparse" for r in result["rows"])


def test_zone_hits_groups_by_zone(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    result = zone_hits.analyze(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7
    )
    zones = {r["zone"] for r in result["hits"]}
    # Fixture seeded events span parking_area_people, alley, back_walkway, 49th_street.
    assert "alley" in zones
    assert "49th_street" in zones


def test_zone_hits_mask_candidates_empty_without_clusters(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    result = zone_hits.analyze(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7
    )
    # Fewer than 5 events per (cam,label) cluster -> no mask candidates.
    assert result["mask_candidates"] == []


def test_score_histogram_uses_triage_subset(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    # Label e1 as tp. With <10 tp samples, branch still falls back to whole-set.
    recorder.record(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path,
        event_id="e1", label="tp",
    )
    result = score_histogram.analyze(
        frigate_db=frigate_db_path, sidecar_db=sidecar_db_path, days=7,
    )
    # alley-overview/person should report n_tp >= 1.
    rows = [r for r in result["rows"] if r["camera"] == "alley-overview" and r["label"] == "person"]
    assert rows
    assert rows[0]["n_tp"] >= 1
