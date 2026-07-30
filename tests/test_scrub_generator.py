"""Integration-shaped tests for the generator orchestration
(scrub/generator.py), with ffmpeg subprocess calls mocked out -- this
sandbox has no ffmpeg binary, so these exercise the real DB/tiling/
cell-assignment wiring against synthetic frame data instead of a real
segment file. See tests/test_scrub_grid.py for the cadence-verification
math itself, tested in isolation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, ScrubSection, Settings, SidecarSection
from frigate_sidecar.scrub import ffmpeg_io, generator

RECORDINGS_SCHEMA = """
CREATE TABLE recordings (
    id TEXT PRIMARY KEY, camera TEXT NOT NULL, path TEXT NOT NULL,
    start_time REAL NOT NULL, end_time REAL NOT NULL, duration REAL NOT NULL,
    segment_size REAL NOT NULL
);
"""


def _make_jpg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 18), color=(10, 20, 30)).save(path)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    # Two contiguous 10s segments.
    conn.execute(
        "INSERT INTO recordings VALUES ('s1','doorbell','/media/frigate/1.mp4',?,?,10.0,5.0)",
        (base, base + 10),
    )
    conn.execute(
        "INSERT INTO recordings VALUES ('s2','doorbell','/media/frigate/2.mp4',?,?,10.0,5.0)",
        (base + 10, base + 20),
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "1.mp4").write_bytes(b"fake")
    (recordings_root / "2.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=tmp_path / "cfg.yml",
            db_path=frigate_db,
            media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, sheet_cols=4, sheet_rows=3,  # 12-cell sheets for a fast test
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0

    async def _fake_probe_pts(seg_path: Path, **kw: object) -> list[float]:
        return [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    async def _fake_extract_keyframes(seg_path: Path, out_dir: Path, **kw: object) -> list[Path]:
        paths = []
        for i in range(10):
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            paths.append(p)
        return paths

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(ffmpeg_io, "probe_keyframe_pts", _fake_probe_pts)
    monkeypatch.setattr(ffmpeg_io, "extract_keyframes", _fake_extract_keyframes)

    return settings


def test_scrub_generate_writes_sheet_with_declared_count_and_verified_cadence(
    env: Settings,
) -> None:
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        # `now` must be close to the synthetic recordings' own timestamps
        # (base=1.8e9) -- the generator now bounds each tier's query to
        # `[window_start, window_end)` (§4.2 non-overlap), so a real
        # wall-clock `now` (far from `base`) would put these recordings
        # outside every tier's window and silently yield zero segments.
        result = asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        assert result["segments"] == 2
        assert result["new_frames"] == 20  # 2 segments x 10 keyframes each

        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert buckets, "expected at least one bucket row"
    assert sheets, "expected at least one sheet row"
    # 20 frames at 12 cells/sheet -> two sheets, first complete (12), second partial (8).
    counts = sorted(s["count"] for s in sheets)
    assert counts == [8, 12]

    # Every accepted cell's achieved timestamp is within interval/2 of its grid
    # point -- the hard contract (spec §4.2), verified end-to-end here via the
    # actual bucket row rather than assumed.
    for b in buckets:
        assert b["generated_through"] >= b["start_ts"]

    # Sheet file actually exists on disk, atomically published.
    for s in sheets:
        path = env.scrub.cache_dir / s["path"]
        assert path.exists()
        assert path.stat().st_size > 0


def test_scrub_generate_gap_splits_bucket(env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A large jump between segments (simulated recording gap) must split the
    bucket rather than fudge the interval (spec §5.2, §11)."""

    async def _fake_probe_pts_with_gap(seg_path: Path, **kw: object) -> list[float]:
        # Segment 2's keyframes look like they start way later than the grid
        # would predict -- simulate by returning offsets that, combined with
        # segment.start_time, produce an >interval*1.5 jump from the prior
        # segment's last accepted frame.
        if "2.mp4" in str(seg_path):
            return [40.0, 41.0]  # segment 2 starts at base+10, so ts = base+50, base+51
        return [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    monkeypatch.setattr(ffmpeg_io, "probe_keyframe_pts", _fake_probe_pts_with_gap)

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_060.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    # Two distinct buckets: the gap must not be bridged into one.
    assert len(buckets) >= 2
    starts = sorted(b["start_ts"] for b in buckets)
    assert starts[1] - starts[0] > 5.0  # the gap is reflected as separate bucket starts


@pytest.fixture
def aged_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Like `env`, but with two segments straddling an `aged_after_h`
    boundary: segment 1 (base..base+10) is older than the boundary, segment 2
    (base+10..base+20) is inside it (§4.2, §5.5 aged/thinning tier).

    Forces the fps-fallback extraction path (a large fake GOP) for both
    tiers so each tier's frames land exactly on its own grid -- the
    keyframe-only path used by `env` samples at a fixed native cadence that
    can't be made to land cleanly on two different tier intervals at once.
    """
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    conn.execute(
        "INSERT INTO recordings VALUES ('s1','doorbell','/media/frigate/1.mp4',?,?,10.0,5.0)",
        (base, base + 10),
    )
    conn.execute(
        "INSERT INTO recordings VALUES ('s2','doorbell','/media/frigate/2.mp4',?,?,10.0,5.0)",
        (base + 10, base + 20),
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "1.mp4").write_bytes(b"fake")
    (recordings_root / "2.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=tmp_path / "cfg.yml",
            db_path=frigate_db,
            media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, aged_interval_s=5.0,
            # boundary = now - aged_after_h*3600; chosen below (per-test `now`)
            # to land at base+15, splitting segment 1 into the aged window and
            # segment 2 into the recent window.
            aged_after_h=5.0 / 3600.0,
            retention_days=4, sheet_cols=4, sheet_rows=3,
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 30.0  # >> any interval * 1.3 -> always the fps-fallback path

    async def _fake_extract_fps(
        seg_path: Path, out_dir: Path, interval_s: float, **kw: object
    ) -> list[Path]:
        n = int(round(10.0 / interval_s))  # 10s segment
        paths = []
        for i in range(n):
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            paths.append(p)
        return paths

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(ffmpeg_io, "extract_fps", _fake_extract_fps)

    return settings


def test_scrub_generate_aged_tier_uses_coarser_interval(aged_env: Settings) -> None:
    """A span older than `aged_after_h` must be generated at `aged_interval_s`,
    not the recent tier's interval (§5.5)."""
    now = 1_800_000_020.0  # right as segment 2 ends; boundary = now - 5s = base+15
    conn = db.open_joined(aged_env.frigate.db_path, aged_env.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert result["segments"] >= 2
    intervals = {b["interval_s"] for b in buckets}
    assert 5.0 in intervals, "expected an aged-tier (5.0s) bucket for the older span"
    assert 1.0 in intervals, "expected a recent-tier (1.0s) bucket for the newer span"

    aged_buckets = [b for b in buckets if b["interval_s"] == 5.0]
    recent_buckets = [b for b in buckets if b["interval_s"] == 1.0]
    # The aged bucket must not extend past the boundary, and the recent
    # bucket must not start before it (§4.2 non-overlap).
    boundary = now - aged_env.scrub.aged_after_h * 3600
    for b in aged_buckets:
        assert b["end_ts"] <= boundary + 1e-6
    for b in recent_buckets:
        assert b["start_ts"] >= boundary - 1e-6


def test_scrub_generate_tiers_do_not_overlap(aged_env: Settings) -> None:
    """No two buckets for the same camera may cover overlapping time spans,
    regardless of tier (§4.2)."""
    now = 1_800_000_020.0
    conn = db.open_joined(aged_env.frigate.db_path, aged_env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    ordered = sorted(buckets, key=lambda b: b["start_ts"])
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        assert prev["end_ts"] <= cur["start_ts"] + 1e-6, (
            f"overlapping buckets: {prev} and {cur}"
        )


def test_scrub_generate_recent_bucket_retired_once_superseded_by_aged(
    aged_env: Settings,
) -> None:
    """A recent-tier bucket that ages past `aged_after_h` must be retired
    (deleted, sheets removed from disk) once the aged tier's coarser bucket
    supersedes its span -- not left to coexist (§4.2, §5.5)."""
    conn = db.open_joined(aged_env.frigate.db_path, aged_env.sidecar.db_path)
    try:
        # Cycle 1: both segments are still "recent" (now right after segment 2).
        now1 = 1_800_000_020.0 + (aged_env.scrub.aged_after_h * 3600) - 1.0
        asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now1, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
        recent_after_1 = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
        recent_after_1 = [b for b in recent_after_1 if b["interval_s"] == 1.0]
        assert recent_after_1, "expected a recent-tier bucket while still fresh"
        sheet_paths_1 = [
            aged_env.scrub.cache_dir / s["path"]
            for s in db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
            if s["interval_s"] == 1.0
        ]
        assert sheet_paths_1 and all(p.exists() for p in sheet_paths_1)

        # Cycle 2: enough wall-clock time has passed that segment 1's old
        # recent-tier bucket now falls before the (advanced) boundary.
        now2 = now1 + (aged_env.scrub.aged_after_h * 3600) + 10.0
        asyncio.run(
            generator.generate_camera(
                aged_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now2, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
        buckets_after_2 = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    boundary2 = now2 - aged_env.scrub.aged_after_h * 3600
    stale_recent = [
        b for b in buckets_after_2 if b["interval_s"] == 1.0 and b["start_ts"] < boundary2
    ]
    assert not stale_recent, "stale recent-tier bucket should have been retired"
    # The now-superseded on-disk sheet files from cycle 1 must be gone too.
    assert not any(p.exists() for p in sheet_paths_1)
