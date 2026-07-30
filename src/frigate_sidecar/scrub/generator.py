"""Continuous scrub-cache generator orchestration (§5).

Ties together: recording enumeration from `frigate.db`, path mapping (§8.2),
GOP-driven sampling (§5.2), cell assignment with gap/drift splitting
(grid.py), tiling (tiling.py), and persistence to the two sidecar tables
(db.py). Designed to be driven either by the continuous ~60s in-process
asyncio task (server.py lifespan, §5.4a) or the `fsc scrub` CLI (§5.7).

Everything on disk lives under `scrub.cache_dir`, including the per-segment
extraction scratch (`.work/`) and the per-sheet cell store (`.cells/`). Both
used to escape it: extraction staged frames in the system temp dir and left
every frame it didn't use behind, and the cell store was never swept at all --
about a gigabyte a day per camera at 1 fps, on the filesystem §8.3 goes out of
its way to keep free. Keeping the scratch inside `cache_dir` also guarantees
the cell moves are same-filesystem renames; across a device boundary they
failed with EXDEV and were silently swallowed, leaving black sheets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from frigate_sidecar import db
from frigate_sidecar.config import Settings
from frigate_sidecar.scrub import ffmpeg_io, grid, tiling
from frigate_sidecar.scrub.mapping import map_recording_path

logger = logging.getLogger(__name__)

# GOP-vs-interval tolerance for choosing keyframe-only decode over full-decode
# fps fallback (§5.2): if the measured GOP is within this factor of the target
# interval, keyframe skipping alone gives a close-enough native cadence to
# rely on cell-assignment's own drift check to catch anything that slips.
_GOP_TOLERANCE = 1.3

_WORK_DIRNAME = ".work"
_CELLS_DIRNAME = ".cells"


@dataclass
class GopCache:
    """Per-camera GOP probe result, checked once per camera per process
    lifetime (one ffprobe call, §5.2) rather than per-segment."""

    seconds: dict[str, float]

    def __init__(self) -> None:
        self.seconds = {}


def _cells_dir(cache_dir: Path, camera: str, interval_s: float, sheet_start: float) -> Path:
    """Per-sheet cell store.

    The sheet start must be rendered losslessly (`grid.fmt_time`, not `%g`):
    at six significant digits every epoch second within the same ~11-day window
    formats to the same string, so *every* sheet of a camera+interval shared one
    directory. Cells are named by their index within a sheet, so sheet N+1's
    cell 000 collided with sheet N's, `_persist_cells` skipped it as already
    present, and the new sheet published the previous sheet's frames.
    """
    return (
        cache_dir
        / camera
        / f"{interval_s:g}"
        / _CELLS_DIRNAME
        / grid.fmt_time(sheet_start)
    )


def _work_root(cache_dir: Path) -> Path:
    work = cache_dir / _WORK_DIRNAME
    work.mkdir(parents=True, exist_ok=True)
    return work


async def _sample_segment(
    seg_path: Path,
    seg_start: float,
    interval_s: float,
    gop_s: float,
    sem: asyncio.Semaphore,
    work_dir: Path,
    *,
    cell_w: int,
    cell_h: int,
) -> list[grid.Frame]:
    """Extract frames from one segment into `work_dir`, returning achieved
    (timestamp, path) pairs. Chooses keyframe-only decode when GOP ~= interval,
    else the full-decode fps fallback (§5.2).

    The caller owns `work_dir` and deletes it once the frames have been either
    accepted into the cell store or discarded, so nothing survives the cycle.
    """
    async with sem:
        if gop_s <= interval_s * _GOP_TOLERANCE:
            pts = await ffmpeg_io.probe_keyframe_pts(seg_path)
            jpgs = await ffmpeg_io.extract_keyframes(
                seg_path, work_dir, cell_w=cell_w, cell_h=cell_h
            )
            n = min(len(pts), len(jpgs))
            return [
                grid.Frame(timestamp=seg_start + pts[i], path=str(jpgs[i])) for i in range(n)
            ]
        jpgs = await ffmpeg_io.extract_fps(
            seg_path, work_dir, interval_s, cell_w=cell_w, cell_h=cell_h
        )
        return [
            grid.Frame(timestamp=seg_start + i * interval_s, path=str(jpgs[i]))
            for i in range(len(jpgs))
        ]


def _publish_sheet_version(
    cache_dir: Path,
    camera: str,
    interval_s: float,
    sheet_start: float,
    count: int,
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    fmt: str,
) -> str:
    """Tile the current cell files for this sheet and atomically publish a new
    immutable version (URL includes `count`, §4.3). Returns the on-disk
    relative path stored in scrub_sheets.path.

    `count` is the sheet's declared cell count and is used verbatim in the
    filename, so the row and the object it points at always agree; cells are
    passed to the tiler with their indices so a missing file leaves its slot
    empty instead of shifting every later frame.
    """
    cells_dir = _cells_dir(cache_dir, camera, interval_s, sheet_start)
    cells: list[tiling.Cell] = [
        (i, p) for i in range(count) if (p := cells_dir / f"{i:03d}.jpg").exists()
    ]

    rel = grid.sheet_rel_path(camera, interval_s, sheet_start, count, grid.ext_for_format(fmt))
    out_path = cache_dir / rel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, prefix=".publish-")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tile = tiling.tile_sheet_webp if fmt == "webp" else tiling.tile_sheet
        tile(cells, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h, out_path=tmp)
        os.replace(tmp, out_path)  # atomic publish (mirrors wildlife.py)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return rel


def _persist_cells(
    cache_dir: Path, camera: str, interval_s: float, sheet_start: float,
    assignments: list[grid.Assignment],
) -> None:
    cells = _cells_dir(cache_dir, camera, interval_s, sheet_start)
    cells.mkdir(parents=True, exist_ok=True)
    for a in assignments:
        dst = cells / f"{a.idx:03d}.jpg"
        if dst.exists():
            continue
        try:
            os.replace(a.path, dst)
        except OSError as exc:
            # Never silent: a failure here is a cell the sheet will render
            # black, and it used to be swallowed whole.
            logger.warning("scrub: could not store cell %s -> %s: %s", a.path, dst, exc)


def _drop_cells_dir(cache_dir: Path, camera: str, interval_s: float, sheet_start: float) -> None:
    """Discard a sheet's cell store once it can never be re-tiled."""
    shutil.rmtree(_cells_dir(cache_dir, camera, interval_s, sheet_start), ignore_errors=True)


def _prune_cell_dirs(cache_dir: Path, camera: str | None, cutoff: float, span_cells: int) -> int:
    """Drop cell stores whose sheet ended before `cutoff`.

    Walked from disk rather than from the DB because a cell store outlives the
    row that produced it (rows are deleted first, then their files).
    """
    if not cache_dir.is_dir():
        return 0
    removed = 0
    cam_dirs = (
        [cache_dir / camera]
        if camera
        else [d for d in cache_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    )
    for cam_dir in cam_dirs:
        if not cam_dir.is_dir():
            continue
        for interval_dir in cam_dir.iterdir():
            if not interval_dir.is_dir():
                continue
            try:
                interval_s = float(interval_dir.name)
            except ValueError:
                continue
            cells_root = interval_dir / _CELLS_DIRNAME
            if not cells_root.is_dir():
                continue
            for sheet_dir in cells_root.iterdir():
                try:
                    sheet_start = float(sheet_dir.name)
                except ValueError:
                    continue
                if sheet_start + span_cells * interval_s < cutoff:
                    shutil.rmtree(sheet_dir, ignore_errors=True)
                    removed += 1
    return removed


def _retire_stale_recent_buckets(
    sidecar_conn: sqlite3.Connection,
    cache_dir: Path,
    camera: str,
    recent_interval_s: float,
    boundary: float,
) -> None:
    """Drop any recent-tier bucket/sheet that now falls (even partially)
    before `boundary` (§4.2 non-overlap, §5.5 thinning).

    `boundary` (= now - aged_after_h) advances every cycle, so a recent
    bucket created when it was still "recent" eventually ages past it. Once
    that happens the aged tier owns that span at its own coarser interval,
    so the finer recent-tier bucket must be retired rather than left to
    overlap it. A bucket straddling the boundary is retired wholesale too
    (rather than clipped) -- the aged tier will regenerate the whole span
    fresh from the underlying recordings, which is simpler than surgically
    splitting an already-tiled sheet, and correct since the source segments
    are still on disk within retention.
    """
    stale = sidecar_conn.execute(
        "SELECT start_ts FROM scrub_buckets WHERE camera = ? AND interval_s = ? AND start_ts < ?",
        (camera, recent_interval_s, boundary),
    ).fetchall()
    if not stale:
        return
    sheet_rows = sidecar_conn.execute(
        "SELECT path, start_ts FROM scrub_sheets "
        "WHERE camera = ? AND interval_s = ? AND start_ts < ?",
        (camera, recent_interval_s, boundary),
    ).fetchall()
    sidecar_conn.execute(
        "DELETE FROM scrub_buckets WHERE camera = ? AND interval_s = ? AND start_ts < ?",
        (camera, recent_interval_s, boundary),
    )
    sidecar_conn.execute(
        "DELETE FROM scrub_sheets WHERE camera = ? AND interval_s = ? AND start_ts < ?",
        (camera, recent_interval_s, boundary),
    )
    sidecar_conn.commit()
    for r in sheet_rows:
        p = cache_dir / r["path"]
        with contextlib.suppress(OSError):
            p.unlink()
        _drop_cells_dir(cache_dir, camera, recent_interval_s, r["start_ts"])


async def _generate_tier(
    settings: Settings,
    camera: str,
    *,
    interval_s: float,
    window_start: float,
    window_end: float,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    gop_cache: GopCache,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Extend one thinning tier's (interval_s's) buckets toward `window_end`,
    never emitting frames outside `[window_start, window_end)` (§4.2
    non-overlap, §5.5 thinning tiers)."""
    scrub = settings.scrub
    cols, rows = scrub.sheet_cols, scrub.sheet_rows
    cell_w, cell_h = scrub.cell_w, scrub.cell_h
    cells_per_sheet = cols * rows

    if window_end <= window_start:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    generated_through = db.latest_generated_through(sidecar_conn, camera, interval_s)
    since = max(generated_through or window_start, window_start)

    # Oldest-first and budgeted: a cold start has no resume point, so `since` is
    # the far edge of the retention window and this query would otherwise return
    # days of segments to chew through before the loop yields.
    rows_ = frigate_conn.execute(
        "SELECT path, start_time, end_time FROM recordings "
        "WHERE camera = ? AND end_time > ? AND start_time < ? ORDER BY start_time LIMIT ?",
        (camera, since, window_end, max(1, scrub.max_segments_per_cycle)),
    ).fetchall()
    if not rows_:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    if camera not in gop_cache.seconds:
        first_path = map_recording_path(
            rows_[0]["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        try:
            gop_cache.seconds[camera] = await ffmpeg_io.probe_gop_seconds(first_path)
        except ffmpeg_io.FfmpegError:
            gop_cache.seconds[camera] = interval_s  # assume best case; drift check will catch it
    gop_s = gop_cache.seconds[camera]

    # Current open bucket: resume the newest incomplete one, or start fresh.
    open_bucket = sidecar_conn.execute(
        "SELECT * FROM scrub_buckets WHERE camera = ? AND interval_s = ? AND complete = 0 "
        "ORDER BY start_ts DESC LIMIT 1",
        (camera, interval_s),
    ).fetchone()
    bucket_start = open_bucket["start_ts"] if open_bucket else None
    bucket_generated_through = open_bucket["generated_through"] if open_bucket else since

    new_frames = 0
    missing_segments = 0
    for row in rows_:
        seg_path = map_recording_path(
            row["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        if not seg_path.exists():
            missing_segments += 1
            continue
        # One scratch dir per segment, inside cache_dir: the extractor writes
        # straight into it and it is removed whether or not the frames were
        # used, so nothing accumulates.
        with tempfile.TemporaryDirectory(
            prefix="extract-", dir=_work_root(scrub.cache_dir)
        ) as td:
            work_dir = Path(td)
            try:
                frames = await _sample_segment(
                    seg_path, row["start_time"], interval_s, gop_s, sem, work_dir,
                    cell_w=cell_w, cell_h=cell_h,
                )
            except ffmpeg_io.FfmpegError as exc:
                logger.warning("scrub: sampling failed for %s: %s", seg_path, exc)
                continue
            frames = [
                f
                for f in frames
                if f.timestamp > bucket_generated_through - interval_s
                and window_start <= f.timestamp < window_end
            ]
            if not frames:
                continue

            pending = frames
            while pending:
                if bucket_start is None:
                    bucket_start = pending[0].timestamp
                elif (pending[0].timestamp - bucket_generated_through) > interval_s * 1.5:
                    # Gap since the last accepted frame in THIS bucket (possibly
                    # from a prior segment) -- must be caught here too, since
                    # assign_cells only sees gaps *within* the frames passed to a
                    # single call (§5.2, §11 gap-splits-bucket test).
                    db.upsert_scrub_bucket(
                        sidecar_conn, camera=camera, start_ts=bucket_start,
                        end_ts=bucket_generated_through + interval_s, interval_s=interval_s,
                        width=cell_w, height=cell_h,
                        generated_through=bucket_generated_through, complete=True,
                    )
                    sidecar_conn.commit()
                    bucket_start = pending[0].timestamp
                result = grid.assign_cells(pending, bucket_start, interval_s)
                new_frames += len(result.accepted)

                if result.accepted:
                    # Route accepted cells into their sheet (a bucket ~30 sheets deep).
                    by_sheet: dict[float, list[grid.Assignment]] = {}
                    for a in result.accepted:
                        sheet_idx_start = bucket_start + (
                            (a.idx // cells_per_sheet) * cells_per_sheet * interval_s
                        )
                        local = grid.Assignment(
                            idx=a.idx - round((sheet_idx_start - bucket_start) / interval_s),
                            timestamp=a.timestamp, path=a.path,
                        )
                        by_sheet.setdefault(sheet_idx_start, []).append(local)

                    for sheet_start, cell_list in by_sheet.items():
                        existing = sidecar_conn.execute(
                            "SELECT COALESCE(MAX(count),0) AS c FROM scrub_sheets "
                            "WHERE camera=? AND interval_s=? AND start_ts=?",
                            (camera, interval_s, sheet_start),
                        ).fetchone()
                        cur_count = int(existing["c"]) if existing else 0
                        if cur_count >= cells_per_sheet:
                            # Already sealed: its cell store is gone, so
                            # re-tiling would publish a blank sheet over it.
                            continue
                        _persist_cells(scrub.cache_dir, camera, interval_s, sheet_start, cell_list)
                        new_count = min(
                            cells_per_sheet, max(cur_count, max(a.idx for a in cell_list) + 1)
                        )
                        complete = new_count >= cells_per_sheet
                        rel = await asyncio.to_thread(
                            _publish_sheet_version,
                            scrub.cache_dir, camera, interval_s, sheet_start,
                            new_count, cols, rows, cell_w, cell_h, scrub.format,
                        )
                        db.upsert_scrub_sheet(
                            sidecar_conn, camera=camera, start_ts=sheet_start,
                            interval_s=interval_s, cols=cols, rows=rows,
                            cell_w=cell_w, cell_h=cell_h,
                            count=new_count, path=rel, complete=complete,
                        )
                        if complete:
                            _drop_cells_dir(scrub.cache_dir, camera, interval_s, sheet_start)

                    bucket_generated_through = max(a.timestamp for a in result.accepted)
                    db.upsert_scrub_bucket(
                        sidecar_conn, camera=camera, start_ts=bucket_start,
                        end_ts=bucket_generated_through + interval_s, interval_s=interval_s,
                        width=cell_w, height=cell_h,
                        generated_through=bucket_generated_through, complete=False,
                    )
                    sidecar_conn.commit()

                if result.split_at is not None:
                    # Seal the current bucket and start a fresh one at split_at.
                    db.upsert_scrub_bucket(
                        sidecar_conn, camera=camera, start_ts=bucket_start,
                        end_ts=bucket_generated_through + interval_s, interval_s=interval_s,
                        width=cell_w, height=cell_h,
                        generated_through=bucket_generated_through, complete=True,
                    )
                    sidecar_conn.commit()
                    bucket_start = None
                    pending = result.remaining
                else:
                    pending = []

    if missing_segments:
        logger.warning(
            "scrub: %d/%d segment file(s) for %s did not resolve under "
            "frigate.recordings_path (%s) -- check the recordings mount (§8.2)",
            missing_segments, len(rows_), camera, settings.frigate.recordings_path,
        )
    return {"camera": camera, "segments": len(rows_), "new_frames": new_frames}


async def generate_camera(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    gop_cache: GopCache,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Extend `camera`'s scrub cache toward `now` by one generation cycle
    (§5.4), across both thinning tiers (§5.5).

    Spans within `aged_after_h` of `now` are generated at the recent tier's
    `recent_interval_s`; older spans (down to `retention_days`) at the
    coarser `aged_interval_s`. The two tiers never overlap (§4.2): a recent
    bucket that ages past the boundary is retired (`_retire_stale_recent_buckets`)
    rather than left to coexist with the aged bucket that now covers its
    span, and a segment straddling the boundary is naturally split between
    the two tier passes below (each pass only accepts frames inside its own
    `[window_start, window_end)`).
    """
    scrub = settings.scrub
    retention_cutoff = now - scrub.retention_days * 86400
    aged_boundary = now - scrub.aged_after_h * 3600
    # Clamp: if aged_after_h is large enough to push the boundary before the
    # retention cutoff, there's no aged window at all -- everything in
    # retention is "recent".
    effective_boundary = max(aged_boundary, retention_cutoff)

    _retire_stale_recent_buckets(
        sidecar_conn, scrub.cache_dir, camera, scrub.recent_interval_s, effective_boundary
    )

    total_segments = 0
    total_new_frames = 0

    if effective_boundary > retention_cutoff:
        aged_result = await _generate_tier(
            settings, camera,
            interval_s=scrub.aged_interval_s,
            window_start=retention_cutoff, window_end=effective_boundary,
            frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
            gop_cache=gop_cache, sem=sem,
        )
        total_segments += aged_result["segments"]
        total_new_frames += aged_result["new_frames"]

    recent_result = await _generate_tier(
        settings, camera,
        interval_s=scrub.recent_interval_s,
        window_start=effective_boundary, window_end=now,
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        gop_cache=gop_cache, sem=sem,
    )
    total_segments += recent_result["segments"]
    total_new_frames += recent_result["new_frames"]

    return {"camera": camera, "segments": total_segments, "new_frames": total_new_frames}


async def generate_cycle(settings: Settings, *, now: float | None = None) -> list[dict[str, Any]]:
    """One generation pass across all opted-in cameras (§5.4)."""
    import time as _time

    now = now if now is not None else _time.time()
    scrub = settings.scrub
    sem = asyncio.Semaphore(scrub.ffmpeg_concurrency)
    gop_cache = GopCache()

    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        cameras = scrub.cameras or [
            r["camera"] for r in conn.execute("SELECT DISTINCT camera FROM recordings").fetchall()
        ]
        results = []
        for camera in cameras:
            try:
                r = await generate_camera(
                    settings, camera, frigate_conn=conn, sidecar_conn=conn,
                    now=now, gop_cache=gop_cache, sem=sem,
                )
            except Exception:
                logger.exception("scrub: generation failed for camera %s", camera)
                r = {"camera": camera, "error": True}
            results.append(r)
        return results
    finally:
        conn.close()


def prune(
    settings: Settings, *, camera: str | None = None, now: float | None = None
) -> dict[str, Any]:
    """Drop sheets/buckets past retention_days, oldest-first (§5.5, §5.7)."""
    import time as _time

    now = now if now is not None else _time.time()
    cutoff = now - settings.scrub.retention_days * 86400
    conn = db.open_sidecar(settings.sidecar.db_path)
    try:
        paths = db.delete_scrub_sheets_before(camera, cutoff, conn)
        n_buckets = db.delete_scrub_buckets_before(conn, camera, cutoff)
        conn.commit()
    finally:
        conn.close()

    n_files = 0
    for rel in paths:
        p = settings.scrub.cache_dir / rel
        with contextlib.suppress(OSError):
            p.unlink()
            n_files += 1
    n_cell_dirs = _prune_cell_dirs(
        settings.scrub.cache_dir,
        camera,
        cutoff,
        settings.scrub.sheet_cols * settings.scrub.sheet_rows,
    )
    return {
        "sheets_deleted": len(paths),
        "files_deleted": n_files,
        "buckets_deleted": n_buckets,
        "cell_dirs_deleted": n_cell_dirs,
    }
