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
class SourceProfile:
    """What we've measured about each camera's stream: keyframe spacing and
    display shape.

    Probed once per camera per process rather than per segment or per cycle --
    neither changes unless the camera is reconfigured, and re-probing every
    cycle cost two ffprobe calls per camera against an ~80s cycle.
    """

    gop_s: dict[str, float]
    aspect: dict[str, float]

    def __init__(self) -> None:
        self.gop_s = {}
        self.aspect = {}


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
            extracted = await ffmpeg_io.extract_keyframes_with_pts(
                seg_path, work_dir, cell_w=cell_w, cell_h=cell_h
            )
            frames = [
                grid.Frame(timestamp=seg_start + pts, path=str(path))
                for pts, path in extracted
            ]
            # Keyframes are only guaranteed to be no *sparser* than the GOP, so
            # a tier whose interval is several GOPs wide gets several frames per
            # cell. Thin them to the target cadence here rather than handing
            # colliding frames to cell assignment, which can only respond by
            # splitting the bucket.
            return grid.decimate_to_grid(frames, interval_s)
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
    since: float,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Sample `[since, window_end)` for one thinning tier, never emitting frames
    outside `[window_start, window_end)` (§4.2 non-overlap, §5.5 thinning tiers).

    `since` is the caller's, not derived from MAX(generated_through) here: the
    scheduler runs this against the live edge and against holes behind it in the
    same cycle, and those passes have entirely different resume points.
    """
    scrub = settings.scrub
    cols, rows = scrub.sheet_cols, scrub.sheet_rows
    cell_w, cell_h = await camera_cell_size(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    cells_per_sheet = cols * rows

    if window_end <= window_start or budget <= 0:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    since = min(max(since, window_start), window_end)

    # Oldest-first and budgeted: a cold start has no resume point, so `since` is
    # the far edge of the retention window and this query would otherwise return
    # days of segments to chew through before the loop yields.
    rows_ = frigate_conn.execute(
        "SELECT path, start_time, end_time FROM recordings "
        "WHERE camera = ? AND end_time > ? AND start_time < ? ORDER BY start_time LIMIT ?",
        (camera, since, window_end, max(1, budget)),
    ).fetchall()
    if not rows_:
        return {"camera": camera, "segments": 0, "new_frames": 0}

    if camera not in profile.gop_s:
        first_path = map_recording_path(
            rows_[0]["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        try:
            profile.gop_s[camera] = await ffmpeg_io.probe_gop_seconds(first_path)
        except ffmpeg_io.FfmpegError:
            profile.gop_s[camera] = interval_s  # assume best case; drift check will catch it
    gop_s = profile.gop_s[camera]

    # Resume an open bucket only if it actually ends where this pass begins.
    # With live-edge and backfill passes interleaved, the newest incomplete
    # bucket is usually the live one; attaching an older backfill pass to it
    # would seal it at the wrong end and hand cell assignment negative indices.
    # Geometry is part of the match: a bucket recorded at one cell size can't be
    # continued at another, or its row would describe cells it doesn't contain
    # and re-tiling would stretch the ones already stored.
    open_bucket = sidecar_conn.execute(
        "SELECT * FROM scrub_buckets WHERE camera = ? AND interval_s = ? AND complete = 0 "
        "AND width = ? AND height = ? "
        "AND generated_through BETWEEN ? AND ? ORDER BY start_ts DESC LIMIT 1",
        (camera, interval_s, cell_w, cell_h,
         since - interval_s * 1.5, since + interval_s * 1.5),
    ).fetchone()
    bucket_start = open_bucket["start_ts"] if open_bucket else None
    bucket_generated_through = open_bucket["generated_through"] if open_bucket else since
    # Grid slot of the newest frame actually stored, or None when nothing has
    # been generated for this tier yet. Distinct from `bucket_generated_through`,
    # which falls back to the window edge -- a moment no frame sits behind, and
    # so not a slot anything should be excluded for.
    last_slot: int | None = (
        round(open_bucket["generated_through"] / interval_s) if open_bucket else None
    )

    new_frames = 0
    missing_segments = 0
    # Sheets touched this cycle, with their declared cell count. Publishing is
    # deferred to sheet completion or the end of the cycle: every distinct count
    # is its own immutable object (§4.3), so publishing once per *segment* wrote
    # a full sheet image for every couple of cells -- ~30x the bytes of the sheet
    # it was building, all of it superseded and all of it kept until retention.
    pending_sheets: dict[float, int] = {}
    dirty_sheets: set[float] = set()

    async def _flush_sheet(sheet_start: float, count: int) -> None:
        complete = count >= cells_per_sheet
        rel = await asyncio.to_thread(
            _publish_sheet_version,
            scrub.cache_dir, camera, interval_s, sheet_start,
            count, cols, rows, cell_w, cell_h, scrub.format,
        )
        db.upsert_scrub_sheet(
            sidecar_conn, camera=camera, start_ts=sheet_start, interval_s=interval_s,
            cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h,
            count=count, path=rel, complete=complete,
        )
        sidecar_conn.commit()
        if complete:
            _drop_cells_dir(scrub.cache_dir, camera, interval_s, sheet_start)

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
            except ffmpeg_io.FfmpegInterrupted as exc:
                # Shutdown took the child with it; not a fault of the segment,
                # and the hole-finder picks it up next cycle.
                logger.debug("scrub: sampling interrupted for %s: %s", seg_path, exc)
                continue
            except ffmpeg_io.FfmpegError as exc:
                logger.warning("scrub: sampling failed for %s: %s", seg_path, exc)
                continue
            # Skip anything already represented: compare *grid slots*, not raw
            # timestamps. Each segment is decimated independently, so a slot
            # straddling a segment boundary gets a candidate from both sides,
            # and handing both to cell assignment forces a bucket split at
            # every boundary.
            frames = [
                f
                for f in frames
                if (last_slot is None or round(f.timestamp / interval_s) > last_slot)
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
                        if sheet_start not in pending_sheets:
                            existing = sidecar_conn.execute(
                                "SELECT COALESCE(MAX(count),0) AS c FROM scrub_sheets "
                                "WHERE camera=? AND interval_s=? AND start_ts=?",
                                (camera, interval_s, sheet_start),
                            ).fetchone()
                            pending_sheets[sheet_start] = int(existing["c"]) if existing else 0
                        if pending_sheets[sheet_start] >= cells_per_sheet:
                            # Already sealed: its cell store is gone, so
                            # re-tiling would publish a blank sheet over it.
                            continue
                        _persist_cells(scrub.cache_dir, camera, interval_s, sheet_start, cell_list)
                        new_count = min(
                            cells_per_sheet,
                            max(pending_sheets[sheet_start], max(a.idx for a in cell_list) + 1),
                        )
                        pending_sheets[sheet_start] = new_count
                        dirty_sheets.add(sheet_start)
                        if new_count >= cells_per_sheet:
                            # Final version of this sheet: nothing more can be
                            # added, so publish now and let the cells go.
                            await _flush_sheet(sheet_start, new_count)
                            dirty_sheets.discard(sheet_start)

                    bucket_generated_through = max(a.timestamp for a in result.accepted)
                    last_slot = round(bucket_generated_through / interval_s)
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

    # One version per still-filling sheet per cycle, not per segment.
    for sheet_start in sorted(dirty_sheets):
        await _flush_sheet(sheet_start, pending_sheets[sheet_start])

    if missing_segments:
        logger.warning(
            "scrub: %d/%d segment file(s) for %s did not resolve under "
            "frigate.recordings_path (%s) -- check the recordings mount (§8.2)",
            missing_segments, len(rows_), camera, settings.frigate.recordings_path,
        )
    return {"camera": camera, "segments": len(rows_), "new_frames": new_frames}


#: How many holes a single cycle will look at before giving up. A span with no
#: recordings behind it (camera offline, motion-only retention) yields nothing
#: and must not stall every older span behind it forever.
_MAX_HOLES_PER_CYCLE = 50


def uncovered_spans(
    sidecar_conn: sqlite3.Connection,
    camera: str,
    interval_s: float,
    window_start: float,
    window_end: float,
) -> list[tuple[float, float]]:
    """Spans of `[window_start, window_end)` with no bucket behind them.

    Holes narrower than one interval are ignored -- they can't hold a frame.
    """
    rows = sidecar_conn.execute(
        "SELECT start_ts, end_ts FROM scrub_buckets "
        "WHERE camera = ? AND interval_s = ? AND end_ts > ? AND start_ts < ? ORDER BY start_ts",
        (camera, interval_s, window_start, window_end),
    ).fetchall()
    spans: list[tuple[float, float]] = []
    cursor = window_start
    for row in rows:
        if row["start_ts"] > cursor + interval_s:
            spans.append((cursor, min(row["start_ts"], window_end)))
        cursor = max(cursor, row["end_ts"])
        if cursor >= window_end:
            break
    if cursor < window_end - interval_s:
        spans.append((cursor, window_end))
    return spans


async def _backfill_tier(
    settings: Settings,
    camera: str,
    *,
    interval_s: float,
    window_start: float,
    window_end: float,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
) -> dict[str, int]:
    """Fill holes in one tier, newest first, each from its newest end.

    §5.4's "backfill fills in behind the live edge": coverage should grow
    backwards from now, contiguously, so a user scrubbing an hour ago is served
    before one scrubbing three days ago. Generation within a hole still runs
    forward (cells accumulate forward), so each pass starts at the oldest of the
    hole's newest `budget` segments and the hole shrinks from the right.

    That start point comes from the recordings table rather than from arithmetic
    on the hole's width: a hole's newest end is frequently dead air (camera
    offline, motion-only retention past the continuous window), and a pass
    aimed there samples nothing and leaves the hole exactly as it found it.
    """
    segments = 0
    frames = 0
    spans = uncovered_spans(sidecar_conn, camera, interval_s, window_start, window_end)
    for hole_start, hole_end in list(reversed(spans))[:_MAX_HOLES_PER_CYCLE]:
        if budget <= 0:
            break
        newest = frigate_conn.execute(
            "SELECT start_time FROM recordings "
            "WHERE camera = ? AND end_time > ? AND start_time < ? "
            "ORDER BY start_time DESC LIMIT 1 OFFSET ?",
            (camera, hole_start, hole_end, budget - 1),
        ).fetchone()
        result = await _generate_tier(
            settings, camera,
            interval_s=interval_s,
            window_start=hole_start, window_end=hole_end,
            # Fewer segments in the hole than the budget -> take it whole.
            since=newest["start_time"] if newest else hole_start,
            budget=budget,
            frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
            profile=profile, sem=sem,
        )
        segments += result["segments"]
        frames += result["new_frames"]
        budget -= result["segments"]
    return {"segments": segments, "new_frames": frames}


def _newest_segment(
    settings: Settings, camera: str, frigate_conn: sqlite3.Connection
) -> Path | None:
    """A segment to probe this camera's stream properties from."""
    row = frigate_conn.execute(
        "SELECT path FROM recordings WHERE camera = ? ORDER BY end_time DESC LIMIT 1",
        (camera,),
    ).fetchone()
    if row is None:
        return None
    seg = map_recording_path(
        row["path"], settings.frigate.media_path, settings.frigate.recordings_path
    )
    return seg if seg.exists() else None


#: Segments pooled to measure a camera's keyframe spacing. One is not enough:
#: a 10s segment holding a 5s GOP contains two keyframes, so it offers a single
#: spacing sample that reads 4.5 or 5.0 purely by where the segment was cut.
#: Measured on this deployment, one camera produced both from consecutive
#: segments -- and since the interval keys every bucket, sheet and directory,
#: that spawned a second tier for the same camera.
_GOP_PROBE_SEGMENTS = 5


async def camera_gop_seconds(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    profile: SourceProfile,
) -> float | None:
    """Median keyframe spacing for `camera`, pooled across recent segments."""
    if camera in profile.gop_s:
        return profile.gop_s[camera]

    rows = frigate_conn.execute(
        "SELECT path FROM recordings WHERE camera = ? ORDER BY end_time DESC LIMIT ?",
        (camera, _GOP_PROBE_SEGMENTS),
    ).fetchall()
    deltas: list[float] = []
    fallback: float | None = None
    for row in rows:
        seg = map_recording_path(
            row["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        if not seg.exists():
            continue
        try:
            found = await ffmpeg_io.probe_keyframe_deltas(seg)
        except ffmpeg_io.FfmpegError:
            continue
        if found:
            deltas.extend(found)
        elif fallback is None:
            # No keyframe pair anywhere in the segment: one per segment at most.
            with contextlib.suppress(ffmpeg_io.FfmpegError):
                fallback = await ffmpeg_io.probe_gop_seconds(seg)

    if not deltas:
        if fallback is None:
            return None
        profile.gop_s[camera] = fallback
        return fallback

    deltas.sort()
    gop = deltas[len(deltas) // 2]
    profile.gop_s[camera] = gop
    return gop


async def camera_cell_size(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    profile: SourceProfile,
) -> tuple[int, int]:
    """Cell dimensions for `camera`: `cell_w` wide, height from the source shape.

    Scaling every camera to a fixed `cell_w x cell_h` squeezes anything whose
    aspect ratio isn't that of the configured cell -- a 4:3 camera rendered into
    a 16:9 cell comes out anamorphically narrowed, and nothing downstream can
    undo it because the pixels are already wrong. The height is therefore
    derived from the source's display aspect, and travels per sheet in the
    `cell_w`/`cell_h` metadata clients already read.

    Falls back to the configured `cell_h` when the shape can't be measured.
    """
    scrub = settings.scrub
    if not scrub.preserve_source_aspect:
        return scrub.cell_w, scrub.cell_h

    aspect = profile.aspect.get(camera)
    if aspect is None:
        seg = _newest_segment(settings, camera, frigate_conn)
        if seg is not None:
            try:
                measured = await ffmpeg_io.probe_display_aspect(seg)
            except ffmpeg_io.FfmpegError:
                measured = None
            if measured:
                profile.aspect[camera] = aspect = measured
    if not aspect:
        return scrub.cell_w, scrub.cell_h

    height = round(scrub.cell_w / aspect)
    height += height % 2  # even dimensions keep the scaler on its fast path
    return scrub.cell_w, max(2, height)


def tier_plan(
    settings: Settings, now: float, gop_s: float | None
) -> list[tuple[float, float, float]]:
    """`[(interval, window_start, window_end), ...]`, newest tier first.

    Normally two tiers: `recent_interval_s` within `aged_after_h` of now, the
    coarser `aged_interval_s` behind it (§5.5).

    When the camera's own keyframe spacing is coarser than `recent_interval_s`,
    the recent tier uses the keyframe cadence instead. Forcing a finer interval
    than the source provides means decoding every frame -- about five times the
    cost of keyframe extraction -- to synthesise stills the encoder never made
    distinct. If that lands at or past `aged_interval_s` the two tiers would
    describe the same cadence, so they collapse into one covering the whole
    retention window; there is nothing left for thinning to save.

    When `coarse_intervals_s` is non-empty, one further tier per entry is
    appended *last* -- after whatever the recent/aged tiers resolved to -- each
    spanning the full retention window (`retention_cutoff` to `now`). Unlike
    the recent/aged pair these deliberately overlap them, and each other
    (§4.2's non-overlap rule only ever applied between recent and aged);
    nothing retires them as spans age, since none is superseded by another
    tier the way recent is by aged. Appending them last keeps `plan[0]`/
    `plan[1]` meaning what every existing caller (`generate_live_edge`'s
    retirement boundary, `generate_camera`'s docstring) already assumes: the
    live/recent tier first, the aged tier -- if any -- second.
    """
    scrub = settings.scrub
    retention_cutoff = now - scrub.retention_days * 86400
    boundary = max(now - scrub.aged_after_h * 3600, retention_cutoff)

    recent = scrub.recent_interval_s
    if scrub.match_keyframe_cadence and gop_s and gop_s > recent * _GOP_TOLERANCE:
        # Snapped to a half-second grid, never used raw. A measured GOP is a
        # median of observed spacings, so it comes out as 4.995056 rather than
        # 5.0 -- and the interval is part of every bucket key, sheet filename
        # and cache directory name. An unsnapped value would put that noise in
        # the URLs, and re-measuring slightly differently later would strand
        # everything generated under the previous one as a separate tier.
        recent = max(recent, round(gop_s * 2.0) / 2.0)

    if recent >= scrub.aged_interval_s:
        plan = [(recent, retention_cutoff, now)]
    else:
        plan = [(recent, boundary, now)]
        if boundary > retention_cutoff:
            plan.append((scrub.aged_interval_s, retention_cutoff, boundary))

    if now > retention_cutoff:
        # Skip a coarse entry that collides with a tier already in the plan:
        # `match_keyframe_cadence` can raise the effective recent interval to
        # exactly a configured coarse value (a ~10 s GOP → recent 10.0), and
        # emitting both would generate the same (interval, window) twice,
        # each pass clobbering the other's bucket resume points.
        existing = {entry[0] for entry in plan}
        for coarse in scrub.coarse_intervals_s:
            if coarse not in existing:
                plan.append((coarse, retention_cutoff, now))
    return plan


async def generate_camera(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Advance `camera`'s scrub cache by one generation cycle (§5.4), across
    both thinning tiers (§5.5).

    Spans within `aged_after_h` of `now` are generated at the recent tier's
    `recent_interval_s`; older spans (down to `retention_days`) at the
    coarser `aged_interval_s`. The two tiers never overlap (§4.2): a recent
    bucket that ages past the boundary is retired (`_retire_stale_recent_buckets`)
    rather than left to coexist with the aged bucket that now covers its
    span, and a segment straddling the boundary is naturally split between
    the two tier passes below (each pass only accepts frames inside its own
    `[window_start, window_end)`).

    **The live edge is serviced first, out of its own budget.** Walking each
    tier forward from the far edge of its window -- which is what "resume from
    MAX(generated_through)" amounts to on a cold cache -- meant the recent tier
    started 24h back and advanced 20 min of footage per cycle while a cycle cost
    ~36 min of wall clock across ten cameras. It lost ground continuously and
    never reached now, so `generated_through` sat a day in the past and the
    client's "is this generated?" check answered no for the one window people
    actually scrub. Holding the edge costs ~6 segments per camera per minute;
    everything left over goes to backfill, which fills in behind it.
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

    live = await generate_live_edge(
        settings, camera, frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        now=now, profile=profile, sem=sem,
    )
    back = await generate_backfill(
        settings, camera, budget=scrub.backfill_segments_per_cycle,
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        now=now, profile=profile, sem=sem,
    )
    return {
        "camera": camera,
        "segments": live["segments"] + back["segments"],
        "new_frames": live["new_frames"] + back["new_frames"],
        "backfilled": back["backfilled"],
    }


async def generate_live_edge(
    settings: Settings,
    camera: str,
    *,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Hold `camera`'s recent tier against wall clock. Cheap and always first.

    The reel opens at the live edge, so that is where sprites have to exist:
    `generated_through` sitting in the past means a client's "is this
    generated?" check answers no for the one window people actually scrub.
    """
    scrub = settings.scrub
    gop_s = await camera_gop_seconds(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    plan = tier_plan(settings, now, gop_s)
    interval_s, window_start, window_end = plan[0]
    # Only retire when a coarser *aged* tier actually exists to supersede this
    # one -- collapsed plans would otherwise delete their own buckets. The
    # (always-overlapping, never-retiring) coarse tiers can also occupy
    # `plan[1]` when recent/aged collapsed into one, so it's not enough to
    # check `len(plan) > 1`; the second entry must actually be the aged tier,
    # distinguishable from every coarse tier by interval (the two can never be
    # equal -- each of `coarse_intervals_s` is validated to exceed
    # `aged_interval_s`).
    if len(plan) > 1 and plan[1][0] not in scrub.coarse_intervals_s:
        _retire_stale_recent_buckets(
            sidecar_conn, scrub.cache_dir, camera, interval_s, window_start
        )

    # Resume from where the recent tier actually reaches, but
    #    never crawl up from further back than `live_edge_lookback_s` -- a
    #    stale cache jumps to the edge and lets backfill close the gap behind
    #    it, rather than making every recent scrub wait for a day of history.
    recent_through = db.latest_generated_through(sidecar_conn, camera, interval_s)
    live_since = max(window_start, now - scrub.live_edge_lookback_s)
    if recent_through is not None and recent_through > live_since:
        live_since = recent_through
    result = await _generate_tier(
        settings, camera,
        interval_s=interval_s,
        window_start=window_start, window_end=window_end,
        since=live_since,
        budget=max(1, scrub.live_edge_segments),
        frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
        profile=profile, sem=sem,
    )
    return {"camera": camera, **result}


async def generate_backfill(
    settings: Settings,
    camera: str,
    *,
    budget: int,
    frigate_conn: sqlite3.Connection,
    sidecar_conn: sqlite3.Connection,
    now: float,
    profile: SourceProfile,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Fill history behind the live edge with whatever budget is left.

    Recent tier first -- it is what someone scrubbing the last day sees -- then
    the coarser aged tier.
    """
    gop_s = await camera_gop_seconds(
        settings, camera, frigate_conn=frigate_conn, profile=profile
    )
    segments = 0
    frames = 0
    remaining = budget
    for interval_s, w_start, w_end in tier_plan(settings, now, gop_s):
        if remaining <= 0:
            break
        if w_end <= w_start:
            continue
        result = await _backfill_tier(
            settings, camera,
            interval_s=interval_s,
            window_start=w_start, window_end=w_end,
            budget=remaining,
            frigate_conn=frigate_conn, sidecar_conn=sidecar_conn,
            profile=profile, sem=sem,
        )
        segments += result["segments"]
        frames += result["new_frames"]
        remaining -= result["segments"]

    return {
        "camera": camera,
        "segments": segments,
        "new_frames": frames,
        # Used its whole share: there is more history behind this camera, so the
        # loop shouldn't sit idle for a full interval before the next cycle.
        "backfilled": budget > 0 and remaining <= 0,
    }


async def generate_cycle(
    settings: Settings,
    *,
    now: float | None = None,
    profile: SourceProfile | None = None,
) -> list[dict[str, Any]]:
    """One generation pass across all opted-in cameras (§5.4).

    Pass a `profile` to keep measured stream properties between cycles; without
    one every cycle re-probes each camera's GOP and aspect, which is two ffprobe
    calls per camera against an ~80s cycle. Callers that want isolation (tests,
    one-shot CLI runs) simply omit it.
    """
    import time as _time

    now = now if now is not None else _time.time()
    started = _time.monotonic()
    scrub = settings.scrub
    sem = asyncio.Semaphore(scrub.ffmpeg_concurrency)
    profile = profile if profile is not None else SourceProfile()

    conn = db.open_joined(settings.frigate.db_path, settings.sidecar.db_path)
    try:
        cameras = scrub.cameras or [
            r["camera"] for r in conn.execute("SELECT DISTINCT camera FROM recordings").fetchall()
        ]
        if not cameras:
            return []

        totals: dict[str, dict[str, Any]] = {
            c: {"camera": c, "segments": 0, "new_frames": 0, "backfilled": False}
            for c in cameras
        }

        def _merge(camera: str, result: dict[str, Any]) -> None:
            totals[camera]["segments"] += result.get("segments", 0)
            totals[camera]["new_frames"] += result.get("new_frames", 0)
            totals[camera]["backfilled"] |= result.get("backfilled", False)

        # Pass 1: every camera's live edge, before any camera's history. Doing
        # both per camera meant the last camera in the list waited out every
        # earlier camera's backfill before its edge was touched at all.
        for camera in cameras:
            try:
                _merge(camera, await generate_live_edge(
                    settings, camera, frigate_conn=conn, sidecar_conn=conn,
                    now=now, profile=profile, sem=sem,
                ))
            except Exception:
                logger.exception("scrub: live edge failed for camera %s", camera)
                totals[camera]["error"] = True

        # Pass 2: backfill on genuine leftovers. Bounded by wall clock as well
        # as segment count -- how long a segment takes depends on the box, and
        # an over-long cycle delays the next live-edge pass, which is what let
        # the edge slip behind in the first place.
        share = max(1, scrub.backfill_segments_per_cycle // len(cameras))
        deadline = _time.monotonic() + scrub.backfill_time_budget_s
        for camera in cameras:
            if _time.monotonic() >= deadline:
                break
            try:
                _merge(camera, await generate_backfill(
                    settings, camera, budget=share, frigate_conn=conn, sidecar_conn=conn,
                    now=now, profile=profile, sem=sem,
                ))
            except Exception:
                logger.exception("scrub: backfill failed for camera %s", camera)
                totals[camera]["error"] = True

        # One line per cycle. Whether the edge is being held is otherwise only
        # visible by querying the DB and inferring it from timestamps, and the
        # cycle's own duration is the floor on how stale the edge can get: a
        # camera is only touched once per cycle.
        elapsed = _time.monotonic() - started
        live_total = sum(r["segments"] for r in totals.values())
        # Worst across cameras of each camera's *newest* bucket. Taking the max
        # over every row instead measures the oldest backfill bucket in the
        # table, which has nothing to do with whether the edge is being held.
        # Across every interval, not just the configured one: a camera whose
        # source is coarser generates at its own cadence, so filtering on
        # `recent_interval_s` would read its stale rows from before that and
        # report a lag that only ever grows.
        lag_row = conn.execute(
            "SELECT MIN(newest) AS furthest FROM ("
            "  SELECT camera, MAX(generated_through) AS newest FROM scrub_buckets"
            "  GROUP BY camera"
            ")"
        ).fetchone()
        # Against wall clock at log time, not the cycle's own `now`: a camera
        # serviced at the start of a 500s cycle is 500s stale by the end, and
        # measuring from `now` would report it as current.
        worst_lag = (
            _time.time() - lag_row["furthest"]
            if lag_row and lag_row["furthest"] is not None
            else float("nan")
        )
        logger.info(
            "scrub: cycle %.0fs, %d cameras, %d segments, %d frames; worst live-edge lag %.0fs",
            elapsed, len(cameras), live_total,
            sum(r["new_frames"] for r in totals.values()), worst_lag,
        )
        if worst_lag > elapsed * 3 and elapsed > 0:
            logger.warning(
                "scrub: live edge is %.0fs behind after a %.0fs cycle -- generation is not "
                "keeping up. Narrow scrub.cameras, or raise scrub.recent_interval_s to at "
                "least the cameras' GOP so keyframe extraction is used instead of a full "
                "decode (see the cycle cost in this log).",
                worst_lag, elapsed,
            )
        return list(totals.values())
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
