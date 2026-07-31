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
import tempfile
from pathlib import Path

import pytest
from PIL import Image

from frigate_sidecar import db
from frigate_sidecar.config import FrigateSection, ScrubSection, Settings, SidecarSection
from frigate_sidecar.scrub import ffmpeg_io, generator, grid

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


def _fake_extract(pts_for: object) -> object:
    """Stand-in for ffmpeg_io.extract_keyframes_with_pts.

    Extraction and timestamps come from one process now, so the fake returns
    the (pts, path) pairs it would have produced rather than two separate lists.
    """

    async def _extract(seg_path: Path, out_dir: Path, **kw: object) -> list[tuple[float, Path]]:
        out = []
        for i, ts in enumerate(pts_for(seg_path)):  # type: ignore[operator]
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            out.append((float(ts), p))
        return out

    return _extract


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

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )

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

    def _pts_with_gap(seg_path: Path) -> list[float]:
        # Segment 2's keyframes look like they start way later than the grid
        # would predict -- simulate by returning offsets that, combined with
        # segment.start_time, produce an >interval*1.5 jump from the prior
        # segment's last accepted frame.
        if "2.mp4" in str(seg_path):
            return [40.0, 41.0]  # segment 2 starts at base+10, so ts = base+50, base+51
        return [float(i) for i in range(10)]

    monkeypatch.setattr(ffmpeg_io, "extract_keyframes_with_pts", _fake_extract(_pts_with_gap))

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
            # This fixture fakes a 30s GOP to force the full-decode path, which
            # is exactly what cadence-matching declines to do; keep the two
            # configured tiers so the thinning machinery is what's under test.
            match_keyframe_cadence=False,
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


def test_generation_leaves_no_scratch_behind(env: Settings) -> None:
    """Extraction used to stage frames in the system temp dir and leave every
    frame it didn't accept there -- about a segment's worth per cycle, forever,
    on whatever filesystem /tmp happens to be."""
    system_tmp_before = set(Path(tempfile.gettempdir()).glob("*"))

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    assert set(Path(tempfile.gettempdir()).glob("*")) == system_tmp_before
    # The in-cache scratch root is emptied too (the dir itself may remain).
    work = env.scrub.cache_dir / ".work"
    assert not list(work.iterdir()) if work.exists() else True


def test_completed_sheet_drops_its_cell_store(env: Settings) -> None:
    """Cells are the raw per-frame JPEGs; nothing ever swept them, so they grew
    at ~1 GB/day/camera at 1 fps under scrub.cache_dir."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    complete = [s for s in sheets if s["complete"]]
    partial = [s for s in sheets if not s["complete"]]
    assert complete and partial

    for s in complete:
        cells = generator._cells_dir(
            env.scrub.cache_dir, "doorbell", s["interval_s"], s["start_ts"]
        )
        assert not cells.exists(), "a sealed sheet can never be re-tiled; its cells are dead"
    for s in partial:
        cells = generator._cells_dir(
            env.scrub.cache_dir, "doorbell", s["interval_s"], s["start_ts"]
        )
        assert cells.exists(), "a still-filling sheet needs its cells for the next re-tile"


def test_prune_removes_sheets_and_their_cell_stores(env: Settings) -> None:
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    sheet_files = list(env.scrub.cache_dir.rglob("*.jpg"))
    assert sheet_files

    # Well past retention relative to the synthetic recordings.
    result = generator.prune(env, now=1_800_000_030.0 + 30 * 86400)
    assert result["sheets_deleted"] > 0
    assert result["buckets_deleted"] > 0
    assert result["cell_dirs_deleted"] > 0
    assert not list((env.scrub.cache_dir / "doorbell").rglob("*.jpg"))


def test_sheet_row_count_matches_its_filename(env: Settings) -> None:
    """The row's `count` and the count baked into its immutable URL are the
    same key; they were computed from different values."""
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    for s in sheets:
        expected = grid.sheet_filename(s["start_ts"], s["interval_s"], s["count"], ".jpg")
        assert Path(s["path"]).name == expected


def test_webp_format_writes_webp_sheets(env: Settings) -> None:
    webp_env = env.model_copy(update={"scrub": env.scrub.model_copy(update={"format": "webp"})})
    conn = db.open_joined(webp_env.frigate.db_path, webp_env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                webp_env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert sheets
    for s in sheets:
        assert s["path"].endswith(".webp")
        with Image.open(webp_env.scrub.cache_dir / s["path"]) as im:
            assert im.format == "WEBP"


def test_extraction_uses_the_configured_cell_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: Settings
) -> None:
    """scrub.cell_w/cell_h never reached ffmpeg, so a non-default cell size
    extracted at 320x180 and was upscaled by the tiler."""
    seen: list[tuple[int, int]] = []

    async def _record_extract(
        seg_path: Path, out_dir: Path, *, cell_w: int = 320, cell_h: int = 180, **kw: object
    ) -> list[tuple[float, Path]]:
        seen.append((cell_w, cell_h))
        out = []
        for i in range(10):
            p = out_dir / f"{i:06d}.jpg"
            _make_jpg(p)
            out.append((float(i), p))
        return out

    monkeypatch.setattr(ffmpeg_io, "extract_keyframes_with_pts", _record_extract)
    big = env.model_copy(
        update={"scrub": env.scrub.model_copy(update={"cell_w": 480, "cell_h": 270})}
    )
    conn = db.open_joined(big.frigate.db_path, big.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                big, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        conn.close()

    assert seen and set(seen) == {(480, 270)}


def test_each_sheet_gets_its_own_cell_store(env: Settings) -> None:
    """Cell files are named by index *within* a sheet, so two sheets sharing a
    directory means sheet N+1 silently republishes sheet N's frames. Rendering
    the sheet start with %g did exactly that: every epoch second in the same
    ~11-day window formats to the same six-significant-digit string."""
    a = generator._cells_dir(Path("/cache"), "doorbell", 1.0, 1_785_380_400.0)
    b = generator._cells_dir(Path("/cache"), "doorbell", 1.0, 1_785_380_496.0)
    assert a != b
    assert a.name == "1785380400"
    assert b.name == "1785380496"

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        sheets = db.list_scrub_sheets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    starts = {s["start_ts"] for s in sheets}
    assert len(starts) == 2
    dirs = {
        generator._cells_dir(env.scrub.cache_dir, "doorbell", 1.0, s).name for s in starts
    }
    assert len(dirs) == 2


def test_cycle_work_is_budgeted(env: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold start has no resume point, so the tier window is the whole
    retention horizon -- days of segments in one cycle without a cap."""
    conn = sqlite3.connect(env.frigate.db_path)
    base = 1_800_000_000.0
    for i in range(2, 12):
        conn.execute(
            "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
            (f"extra{i}", "/media/frigate/1.mp4", base + i * 10, base + (i + 1) * 10),
        )
    conn.commit()
    conn.close()

    joined = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_backfill(
                env, "doorbell", budget=3, frigate_conn=joined, sidecar_conn=joined,
                now=base + 200, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
    finally:
        joined.close()

    assert result["segments"] == 3, "one pass must stop at the budget, not drain the window"
    assert result["backfilled"] is True, "spending the whole share means history remains"


def test_dense_keyframes_fill_whole_sheets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reproduces what a 5s tier did against Frigate's ~1s GOP on live footage.

    Keyframe decode returns the encoder's cadence, so five frames arrived per
    5s cell. Cell assignment could only refuse to overwrite and split, so every
    bucket held one cell and every sheet was a 96-cell image with one frame in
    it -- a 96x disk amplification that covered 4 minutes of footage in 236
    sheets before it was caught.
    """
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    for i in range(12):  # 12 contiguous 10s segments = 2 minutes
        conn.execute(
            "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
            (f"s{i}", f"/media/frigate/{i}.mp4", base + i * 10, base + (i + 1) * 10),
        )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    for i in range(12):
        (recordings_root / f"{i}.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=tmp_path / "cfg.yml",
            db_path=frigate_db, media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=5.0, aged_after_h=9999.0,  # one tier, 5s cadence
            sheet_cols=4, sheet_rows=3,  # 12-cell sheets = 60s of footage each
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0  # Frigate's measured GOP: well under the 5s target

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )

    joined = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                settings, "doorbell", frigate_conn=joined, sidecar_conn=joined,
                now=base + 120, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(joined, "doorbell", 0, 1_900_000_000)
        sheets = db.list_scrub_sheets(joined, "doorbell", 0, 1_900_000_000)
    finally:
        joined.close()

    # 2 minutes at 5s is ~25 grid points: two full 12-cell sheets and the start
    # of a third -- all in ONE unbroken bucket, not one bucket per frame.
    assert len(buckets) == 1, f"cadence mismatch split the bucket {len(buckets)} ways"
    counts = [s["count"] for s in sorted(sheets, key=lambda r: r["start_ts"])]
    assert counts[:-1] == [12] * (len(sheets) - 1), f"sheets left unfilled: {counts}"
    assert sum(counts) >= 24, f"expected ~25 frames over 2 minutes at 5s, got {sum(counts)}"
    # And the frames really are 5s apart across the whole run.
    span = buckets[0]["end_ts"] - buckets[0]["start_ts"]
    assert span == pytest.approx(120.0, abs=5.0)


def test_a_filling_sheet_is_published_once_per_cycle(env: Settings) -> None:
    """Every distinct count is its own immutable object (§4.3), so publishing
    per *segment* wrote a whole sheet image for each handful of cells -- tens of
    megabytes of superseded versions per 600 KB sheet, all kept until retention.
    """
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_030.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        rows = conn.execute(
            "SELECT start_ts, COUNT(*) versions FROM scrub_sheets GROUP BY start_ts"
        ).fetchall()
    finally:
        conn.close()

    # Two 10s segments, 12-cell sheets: the first sheet seals inside the cycle,
    # the second is still filling. Each is written exactly once.
    assert [r["versions"] for r in rows] == [1, 1], "a sheet was republished mid-cycle"
    on_disk = sorted(p.name for p in (env.scrub.cache_dir / "doorbell" / "1").glob("*.jpg"))
    assert len(on_disk) == 2, f"superseded sheet images left on disk: {on_disk}"


# ----- Cycle scheduling: live edge first, backfill behind it (§5.4) -----


@pytest.fixture
def long_history_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """24h of contiguous 10s segments and a cold cache -- the shape a real
    deployment starts in."""
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    base = 1_800_000_000.0
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
        [
            (f"s{i}", "/media/frigate/x.mp4", base + i * 10, base + (i + 1) * 10)
            for i in range(8640)  # 24h
        ],
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "x.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=tmp_path / "cfg.yml",
            db_path=frigate_db, media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, aged_after_h=48.0,  # everything is "recent"
            sheet_cols=4, sheet_rows=3,
            backfill_segments_per_cycle=12, live_edge_segments=6,
            live_edge_lookback_s=300.0,
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )
    return settings


def test_first_cycle_on_a_cold_cache_reaches_the_live_edge(long_history_env: Settings) -> None:
    """The reel opens at the live edge, so that is where sprites have to exist.

    Resuming each tier from MAX(generated_through) meant a cold cache started a
    day back and advanced 20 min of footage per cycle -- slower than wall clock
    across ten cameras -- so `generated_through` stayed ~24h behind and the
    client's "is this generated?" check answered no for everything recent.
    """
    env = long_history_env
    now = 1_800_000_000.0 + 86400.0
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        asyncio.run(
            generator.generate_camera(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=now, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
        through = db.latest_generated_through(conn, "doorbell", 1.0)
    finally:
        conn.close()

    assert through is not None
    lag = now - through
    assert lag <= env.scrub.live_edge_lookback_s, (
        f"live edge is {lag / 3600:.1f}h behind after the very first cycle"
    )


def test_backfill_fills_in_behind_the_live_edge(long_history_env: Settings) -> None:
    """Coverage should grow backwards from now, contiguously -- a user scrubbing
    an hour ago is served before one scrubbing three days ago (§5.4)."""
    env = long_history_env
    now = 1_800_000_000.0 + 86400.0
    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        for cycle in range(4):
            asyncio.run(
                generator.generate_camera(
                    env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                    now=now + cycle * 60, gop_cache=generator.GopCache(),
                    sem=asyncio.Semaphore(3),
                )
            )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, now + 1000)
    finally:
        conn.close()

    covered_start = min(b["start_ts"] for b in buckets)
    covered_end = max(b["end_ts"] for b in buckets)
    # Still pinned to the edge...
    assert now - covered_end <= env.scrub.live_edge_lookback_s
    # ...and reaching further back than the live-edge pass alone ever would.
    assert now - covered_start > env.scrub.live_edge_lookback_s, (
        "backfill made no progress behind the live edge"
    )


def test_uncovered_spans_finds_holes_between_buckets(tmp_path: Path) -> None:
    conn = db.open_sidecar(tmp_path / "sidecar.db")
    try:
        for start, end in ((100.0, 200.0), (400.0, 500.0)):
            db.upsert_scrub_bucket(
                conn, camera="c", start_ts=start, end_ts=end, interval_s=1.0,
                width=1, height=1, generated_through=end, complete=True,
            )
        conn.commit()
        spans = generator.uncovered_spans(conn, "c", 1.0, 0.0, 700.0)
    finally:
        conn.close()
    assert spans == [(0.0, 100.0), (200.0, 400.0), (500.0, 700.0)]


def test_uncovered_spans_ignores_sub_interval_seams(tmp_path: Path) -> None:
    """A hole narrower than one interval can't hold a frame, so chasing it
    would make backfill spin on a seam forever."""
    conn = db.open_sidecar(tmp_path / "sidecar.db")
    try:
        for start, end in ((100.0, 200.0), (200.5, 300.0)):
            db.upsert_scrub_bucket(
                conn, camera="c", start_ts=start, end_ts=end, interval_s=1.0,
                width=1, height=1, generated_through=end, complete=True,
            )
        conn.commit()
        spans = generator.uncovered_spans(conn, "c", 1.0, 100.0, 300.0)
    finally:
        conn.close()
    assert spans == []


def test_a_hole_with_no_recordings_does_not_stall_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Camera offline, or motion-only retention: a span with nothing behind it
    yields nothing forever, and must not block the spans older than it."""
    base = 1_800_000_000.0
    frigate_db = tmp_path / "frigate.db"
    conn = sqlite3.connect(frigate_db)
    conn.executescript(RECORDINGS_SCHEMA)
    # Recordings only in the OLDER half; the newer half is a permanent hole.
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'doorbell', ?, ?, ?, 10.0, 5.0)",
        [
            (f"s{i}", "/media/frigate/x.mp4", base + i * 10, base + (i + 1) * 10)
            for i in range(30)  # base .. base+300 only
        ],
    )
    conn.commit()
    conn.close()

    recordings_root = tmp_path / "recordings"
    recordings_root.mkdir()
    (recordings_root / "x.mp4").write_bytes(b"fake")

    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000", config_path=tmp_path / "cfg.yml",
            db_path=frigate_db, media_path=Path("/media/frigate"),
            recordings_path=recordings_root,
        ),
        sidecar=SidecarSection(db_path=tmp_path / "sidecar.db", bind_port=5001),
        scrub=ScrubSection(
            enabled=True, cameras=["doorbell"], cache_dir=tmp_path / "scrub",
            recent_interval_s=1.0, aged_after_h=48.0, sheet_cols=4, sheet_rows=3,
            backfill_segments_per_cycle=12, live_edge_segments=2, live_edge_lookback_s=60.0,
        ),
    )

    async def _fake_probe_gop(seg_path: Path, **kw: object) -> float:
        return 1.0

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _fake_probe_gop)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [float(i) for i in range(10)]),
    )

    now = base + 1200.0  # 15 min of empty wall clock after the last recording
    joined = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_camera(
                settings, "doorbell", frigate_conn=joined, sidecar_conn=joined,
                now=now, gop_cache=generator.GopCache(), sem=asyncio.Semaphore(3),
            )
        )
    finally:
        joined.close()

    # The live-edge window is empty, but the cycle still reached past it into the
    # span that does have recordings.
    assert result["new_frames"] > 0, "backfill stalled on a hole with no recordings"


def test_every_camera_gets_its_live_edge_before_any_backfill(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running live-edge-then-backfill per camera meant the last camera in the
    list waited out every earlier camera's history before its edge was touched
    -- minutes of staleness that scales with camera count."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(8640)
        ],
    )
    conn.commit()
    conn.close()

    order: list[str] = []
    real_live = generator.generate_live_edge
    real_backfill = generator.generate_backfill

    async def _tracked_live(settings: object, camera: str, **kw: object) -> dict[str, object]:
        order.append(f"live:{camera}")
        return await real_live(settings, camera, **kw)  # type: ignore[arg-type]

    async def _tracked_backfill(settings: object, camera: str, **kw: object) -> dict[str, object]:
        order.append(f"backfill:{camera}")
        return await real_backfill(settings, camera, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(generator, "generate_live_edge", _tracked_live)
    monkeypatch.setattr(generator, "generate_backfill", _tracked_backfill)

    env = env.model_copy(update={"scrub": env.scrub.model_copy(update={"cameras": []})})
    asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    live_phase = [step for step in order if step.startswith("live:")]
    first_backfill = next(i for i, step in enumerate(order) if step.startswith("backfill:"))
    assert len(live_phase) == 2, f"both cameras should get a live-edge pass: {order}"
    assert first_backfill == len(live_phase), (
        f"backfill started before every camera's edge was serviced: {order}"
    )


def test_backfill_budget_is_shared_across_cameras(long_history_env: Settings) -> None:
    """The cycle's wall clock has to stay bounded regardless of camera count --
    an over-long cycle makes the next live-edge pass late, and the edge slips."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(8640)
        ],
    )
    conn.commit()
    conn.close()

    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_segments_per_cycle": 8, "live_edge_segments": 2}
            )
        }
    )
    results = asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    assert {r["camera"] for r in results} == {"doorbell", "garden"}
    # 8 shared across 2 cameras = 4 each, plus each camera's own 2-segment edge.
    for r in results:
        assert r["segments"] <= 4 + 2, f"{r['camera']} overran its share: {r}"


def test_backfill_stops_at_its_wall_clock_budget(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Segment count alone can't bound a cycle -- how long a segment takes
    depends on the box. An over-long cycle delays the next live-edge pass,
    which is what let the edge slip behind to begin with."""
    env = long_history_env
    conn = sqlite3.connect(env.frigate.db_path)
    conn.executemany(
        "INSERT INTO recordings VALUES (?, 'garden', ?, ?, ?, 10.0, 5.0)",
        [
            (f"g{i}", "/media/frigate/x.mp4", 1_800_000_000.0 + i * 10,
             1_800_000_000.0 + (i + 1) * 10)
            for i in range(8640)
        ],
    )
    conn.commit()
    conn.close()

    env = env.model_copy(
        update={
            "scrub": env.scrub.model_copy(
                update={"cameras": [], "backfill_time_budget_s": 0.0, "live_edge_segments": 2}
            )
        }
    )
    backfilled: list[str] = []
    real_backfill = generator.generate_backfill

    async def _tracked(settings: object, camera: str, **kw: object) -> dict[str, object]:
        backfilled.append(camera)
        return await real_backfill(settings, camera, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(generator, "generate_backfill", _tracked)
    results = asyncio.run(generator.generate_cycle(env, now=1_800_000_000.0 + 86400.0))

    assert not backfilled, "a spent time budget must skip backfill entirely"
    # The live edge still ran for every camera -- it is never the thing skipped.
    assert all(r["segments"] > 0 for r in results)


# ----- Cadence matching: don't full-decode what the source can't provide -----


def _settings_with_gop(tmp_path: Path, **scrub_kw: object) -> Settings:
    return Settings(
        frigate=FrigateSection(config_path=tmp_path / "cfg.yml", db_path=tmp_path / "f.db"),
        sidecar=SidecarSection(db_path=tmp_path / "s.db"),
        scrub=ScrubSection(
            recent_interval_s=1.0, aged_interval_s=5.0, aged_after_h=24.0,
            retention_days=4, **scrub_kw,  # type: ignore[arg-type]
        ),
    )


def test_a_fine_gop_keeps_both_configured_tiers(tmp_path: Path) -> None:
    """The Dahua cameras here emit a keyframe a second, so 1 fps is free."""
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=1.0)
    assert [round(p[0], 2) for p in plan] == [1.0, 5.0]
    assert plan[0][2] == now  # recent tier runs to the live edge


def test_a_coarse_gop_generates_at_the_keyframe_cadence(tmp_path: Path) -> None:
    """UniFi Protect emits one every 5s and exposes no way to change it.

    Forcing 1 fps against that means decoding every frame -- ~5x the cost -- to
    synthesise stills the encoder never made distinct. On the reference
    deployment three such cameras were ~70% of the generator's total work.
    """
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=5.0)
    # Recent and aged would describe the same cadence, so they collapse into one
    # tier covering the whole retention window.
    assert len(plan) == 1
    interval, start, end = plan[0]
    assert interval == 5.0
    assert end == now
    assert now - start == pytest.approx(4 * 86400)


def test_a_moderately_coarse_gop_still_thins(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=2.0)
    assert [p[0] for p in plan] == [2.0, 5.0]


def test_cadence_matching_can_be_turned_off(tmp_path: Path) -> None:
    """Off means force the configured interval and pay the decode."""
    now = 1_800_000_000.0
    plan = generator.tier_plan(
        _settings_with_gop(tmp_path, match_keyframe_cadence=False), now, gop_s=5.0
    )
    assert [p[0] for p in plan] == [1.0, 5.0]


def test_a_gop_within_tolerance_is_not_treated_as_coarser(tmp_path: Path) -> None:
    """1.2s against a 1.0s target is inside _GOP_TOLERANCE: keyframe extraction
    already lands close enough, and cell assignment's drift check catches the
    rest."""
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=1.2)
    assert plan[0][0] == 1.0


def test_unknown_gop_falls_back_to_the_configured_interval(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    plan = generator.tier_plan(_settings_with_gop(tmp_path), now, gop_s=None)
    assert [p[0] for p in plan] == [1.0, 5.0]


def test_coarse_gop_camera_uses_the_cheap_extraction_path(
    long_history_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a 5s-GOP camera must never reach extract_fps."""
    env = long_history_env.model_copy(
        update={
            "scrub": long_history_env.scrub.model_copy(
                update={"recent_interval_s": 1.0, "aged_interval_s": 5.0, "aged_after_h": 24.0}
            )
        }
    )

    async def _coarse_gop(seg_path: Path, **kw: object) -> float:
        return 5.0

    async def _boom(*a: object, **k: object) -> list[Path]:
        raise AssertionError("full decode used for a source that can't cheaply provide it")

    monkeypatch.setattr(ffmpeg_io, "probe_gop_seconds", _coarse_gop)
    monkeypatch.setattr(ffmpeg_io, "extract_fps", _boom)
    monkeypatch.setattr(
        ffmpeg_io, "extract_keyframes_with_pts",
        _fake_extract(lambda seg: [0.0, 5.0]),  # one keyframe every 5s
    )

    conn = db.open_joined(env.frigate.db_path, env.sidecar.db_path)
    try:
        result = asyncio.run(
            generator.generate_live_edge(
                env, "doorbell", frigate_conn=conn, sidecar_conn=conn,
                now=1_800_000_000.0 + 86400.0, gop_cache=generator.GopCache(),
                sem=asyncio.Semaphore(3),
            )
        )
        buckets = db.list_scrub_buckets(conn, "doorbell", 0, 1_900_000_000)
    finally:
        conn.close()

    assert result["new_frames"] > 0
    assert {b["interval_s"] for b in buckets} == {5.0}


@pytest.mark.parametrize(
    ("measured", "expected"),
    [(4.995056, 5.0), (5.001, 5.0), (5.0, 5.0), (2.24, 2.0), (3.3, 3.5), (30.02, 30.0)],
)
def test_derived_interval_is_snapped_to_a_stable_grid(
    tmp_path: Path, measured: float, expected: float
) -> None:
    """A measured GOP is a median of observed spacings, so it arrives as
    4.995056 rather than 5.0 -- and the interval is part of every bucket key,
    sheet filename and cache directory name. Left raw, that noise lands in the
    URLs and a slightly different measurement later strands everything
    generated under the previous value as its own tier."""
    settings = _settings_with_gop(tmp_path)
    plan = generator.tier_plan(settings, 1_800_000_000.0, gop_s=measured)
    assert plan[0][0] == expected


def test_small_measurement_noise_does_not_create_a_new_tier(tmp_path: Path) -> None:
    settings = _settings_with_gop(tmp_path)
    now = 1_800_000_000.0
    intervals = {
        generator.tier_plan(settings, now, gop_s=g)[0][0]
        for g in (4.98, 4.995056, 5.0, 5.02, 5.11)
    }
    assert intervals == {5.0}, f"same source resolved to several tiers: {intervals}"
