from __future__ import annotations

from pathlib import Path

from frigate_sidecar.triage import recorder, sampler


def test_sample_quota_for_n() -> None:
    q = sampler.SampleQuota.for_n(30)
    assert q.decision_zone + q.low_tail + q.high_tail == 30
    assert q.decision_zone == 18  # 60%
    assert q.low_tail == 6  # 20%
    assert q.high_tail == 6  # remainder


def test_score_band() -> None:
    assert sampler.score_band(0.90) == "high"
    assert sampler.score_band(0.85) == "high"
    assert sampler.score_band(0.80) == "midhigh"
    assert sampler.score_band(0.70) == "mid"
    assert sampler.score_band(0.60) == "low"
    assert sampler.score_band(0.50) == "very_low"


def test_sample_returns_events_within_window(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        seed=1,
    )
    # 4 events are within 7 days in the fixture (e5 is 30 days old).
    ids = {ev["id"] for ev in out}
    assert ids == {"e1", "e2", "e3", "e4"}
    # Each event has a snapshot URL anchored at the configured base.
    for ev in out:
        assert ev["snapshot_url"].startswith("http://example.test:5000/api/events/")


def test_sample_skips_already_labeled(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    # Pre-label e1 so the sampler should skip it.
    recorder.record(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        event_id="e1",
        label="fp",
    )
    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        seed=1,
    )
    assert "e1" not in {ev["id"] for ev in out}


def test_sample_camera_filter(
    frigate_db_path: Path, sidecar_db_path: Path
) -> None:
    out = sampler.sample(
        frigate_db=frigate_db_path,
        sidecar_db=sidecar_db_path,
        api_base_url="http://example.test:5000",
        days=7,
        n=10,
        camera="alley-overview",
        seed=1,
    )
    assert {ev["camera"] for ev in out} == {"alley-overview"}
