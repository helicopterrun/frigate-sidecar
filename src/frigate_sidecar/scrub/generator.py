"""Continuous scrub-cache generator orchestration (§5).

Ties together: recording enumeration from `frigate.db`, path mapping (§8.2),
GOP-driven sampling (§5.2), cell assignment with gap/drift splitting
(grid.py), tiling (tiling.py), and persistence to the two sidecar tables
(db.py). Designed to be driven either by the continuous ~60s in-process
asyncio task (server.py lifespan, §5.4a) or the `fsc scrub` CLI (§5.7).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
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


@dataclass
class GopCache:
    """Per-camera GOP probe result, checked once per camera per process
    lifetime (one ffprobe call, §5.2) rather than per-segment."""

    seconds: dict[str, float]

    def __init__(self) -> None:
        self.seconds = {}


def _cells_dir(cache_dir: Path, camera: str, interval_s: float, sheet_start: float) -> Path:
    return cache_dir / camera / f"{interval_s:g}" / ".cells" / f"{sheet_start:g}"


async def _sample_segment(
    seg_path: Path, seg_start: float, interval_s: float, gop_s: float, sem: asyncio.Semaphore
) -> list[grid.Frame]:
    """Extract frames from one segment, returning achieved (timestamp, path)
    pairs. Chooses keyframe-only decode when GOP ~= interval, else the
    full-decode fps fallback (§5.2)."""
    async with sem:
        with tempfile.TemporaryDirectory(prefix="scrub-extract-") as td:
            tmp = Path(td)
            if gop_s <= interval_s * _GOP_TOLERANCE:
                pts = await ffmpeg_io.probe_keyframe_pts(seg_path)
                jpgs = await ffmpeg_io.extract_keyframes(seg_path, tmp)
                n = min(len(pts), len(jpgs))
                frames = [
                    grid.Frame(timestamp=seg_start + pts[i], path=str(jpgs[i])) for i in range(n)
                ]
            else:
                jpgs = await ffmpeg_io.extract_fps(seg_path, tmp, interval_s)
                frames = [
                    grid.Frame(timestamp=seg_start + i * interval_s, path=str(jpgs[i]))
                    for i in range(len(jpgs))
                ]
            # Copy extracted frames out of the temp dir before it's cleaned up
            # -- caller persists the ones it actually accepts into cells/.
            out = []
            for f in frames:
                persisted = tmp.parent / f"{os.getpid()}-{Path(f.path).name}"
                # tmp will be removed on context exit; copy bytes now so the
                # returned paths remain valid to the caller.
                data = Path(f.path).read_bytes()
                persisted.write_bytes(data)
                out.append(grid.Frame(timestamp=f.timestamp, path=str(persisted)))
            return out


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
    relative path stored in scrub_sheets.path."""
    cells = _cells_dir(cache_dir, camera, interval_s, sheet_start)
    cell_paths = [cells / f"{i:03d}.jpg" for i in range(count)]
    cell_paths = [p for p in cell_paths if p.exists()]

    rel = grid.sheet_rel_path(camera, interval_s, sheet_start, len(cell_paths))
    out_path = cache_dir / rel
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent if out_path.parent.exists() else cache_dir)
    os.close(fd)
    tmp = Path(tmp_name)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "webp":
        tiling.tile_sheet_webp(
            cell_paths, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h, out_path=tmp
        )
    else:
        tiling.tile_sheet(
            cell_paths, cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h, out_path=tmp
        )
    os.replace(tmp, out_path)  # atomic publish (mirrors wildlife.py)
    return rel


def _persist_cells(
    cache_dir: Path, camera: str, interval_s: float, sheet_start: float,
    assignments: list[grid.Assignment],
) -> None:
    cells = _cells_dir(cache_dir, camera, interval_s, sheet_start)
    cells.mkdir(parents=True, exist_ok=True)
    for a in assignments:
        dst = cells / f"{a.idx:03d}.jpg"
        if not dst.exists():
            with contextlib.suppress(OSError):
                Path(a.path).replace(dst)


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
    (§5.4). Safe to call repeatedly / concurrently across cameras."""
    scrub = settings.scrub
    interval_s = scrub.recent_interval_s
    cols, rows = scrub.sheet_cols, scrub.sheet_rows
    cell_w, cell_h = scrub.cell_w, scrub.cell_h
    retention_cutoff = now - scrub.retention_days * 86400

    generated_through = db.latest_generated_through(sidecar_conn, camera)
    since = max(generated_through or retention_cutoff, retention_cutoff)

    rows_ = frigate_conn.execute(
        "SELECT path, start_time, end_time FROM recordings "
        "WHERE camera = ? AND end_time > ? ORDER BY start_time",
        (camera, since),
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

    # Current open (still-filling) sheet for this bucket.
    def _open_sheet(start: float) -> sqlite3.Row | None:
        return sidecar_conn.execute(
            "SELECT * FROM scrub_sheets WHERE camera = ? AND interval_s = ? "
            "AND complete = 0 AND start_ts >= ? ORDER BY start_ts DESC LIMIT 1",
            (camera, interval_s, start),
        ).fetchone()

    new_frames = 0
    for row in rows_:
        seg_path = map_recording_path(
            row["path"], settings.frigate.media_path, settings.frigate.recordings_path
        )
        if not seg_path.exists():
            continue
        try:
            frames = await _sample_segment(seg_path, row["start_time"], interval_s, gop_s, sem)
        except ffmpeg_io.FfmpegError as exc:
            logger.warning("scrub: sampling failed for %s: %s", seg_path, exc)
            continue
        frames = [f for f in frames if f.timestamp > bucket_generated_through - interval_s]
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
                if bucket_start is not None:
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
                sheet_span = cols * rows * interval_s
                for a in result.accepted:
                    sheet_idx_start = bucket_start + (
                        (a.idx // (cols * rows)) * cols * rows * interval_s
                    )
                    local = grid.Assignment(
                        idx=a.idx - round((sheet_idx_start - bucket_start) / interval_s),
                        timestamp=a.timestamp, path=a.path,
                    )
                    by_sheet.setdefault(sheet_idx_start, []).append(local)

                for sheet_start, cell_list in by_sheet.items():
                    _persist_cells(scrub.cache_dir, camera, interval_s, sheet_start, cell_list)
                    existing = sidecar_conn.execute(
                        "SELECT COALESCE(MAX(count),0) AS c FROM scrub_sheets "
                        "WHERE camera=? AND interval_s=? AND start_ts=?",
                        (camera, interval_s, sheet_start),
                    ).fetchone()
                    cur_count = int(existing["c"]) if existing else 0
                    max_idx = max(a.idx for a in cell_list)
                    new_count = max(cur_count, max_idx + 1)
                    complete = new_count >= cols * rows
                    rel = _publish_sheet_version(
                        scrub.cache_dir, camera, interval_s, sheet_start,
                        new_count, cols, rows, cell_w, cell_h, scrub.format,
                    )
                    db.upsert_scrub_sheet(
                        sidecar_conn, camera=camera, start_ts=sheet_start, interval_s=interval_s,
                        cols=cols, rows=rows, cell_w=cell_w, cell_h=cell_h,
                        count=new_count, path=rel, complete=complete,
                    )
                    _ = sheet_span  # documented above; not otherwise needed here

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
                if bucket_start is not None:
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

    return {"camera": camera, "segments": len(rows_), "new_frames": new_frames}


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
    return {"sheets_deleted": len(paths), "files_deleted": n_files, "buckets_deleted": n_buckets}
