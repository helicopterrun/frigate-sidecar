"""Pure grid/URL/cell-assignment math for the scrub cache.

Deliberately free of I/O so the cadence-verification and gap-splitting rules
(docs/scrub-cache-and-proxy-spec.md §4.2, §5.2, and the highest-value test in
§11) can be exercised without ffmpeg or a real segment file.
"""

from __future__ import annotations

from dataclasses import dataclass


def grid_point(start: float, interval: float, k: int) -> float:
    """The k-th grid timestamp for a bucket starting at `start`."""
    return start + k * interval


def cell_index(t: float, start: float, interval: float) -> int:
    """Row-major cell index for wall-clock time `t` (spec §4.3)."""
    return round((t - start) / interval)


def within_bound(achieved: float, start: float, interval: float, k: int) -> bool:
    """True if `achieved` is within interval/2 of grid point k (spec §4.2)."""
    return abs(achieved - grid_point(start, interval, k)) <= interval / 2 + 1e-9


def sheet_filename(start: float, interval: float, count: int) -> str:
    """Content-addressed filename -- (start, interval, count) is the whole key
    (spec §4.3 finding 3: count MUST be in the name so a growing live sheet
    never reuses an immutable URL)."""

    def _fmt_start(x: float) -> str:
        # Bucket/sheet starts land on whole seconds in practice; render as an
        # integer when exact (matches the spec's own examples, e.g.
        # "1785380400-1.0-96.jpg"), otherwise keep full precision.
        return str(int(x)) if x == int(x) else repr(float(x))

    def _fmt_interval(x: float) -> str:
        # Interval always carries a decimal point (spec examples: "1.0", "5.0").
        return f"{float(x):.1f}" if x == int(x) else repr(float(x))

    return f"{_fmt_start(start)}-{_fmt_interval(interval)}-{count}.jpg"


def sheet_rel_path(camera: str, interval: float, start: float, count: int) -> str:
    """On-disk path under scrub.cache_dir (spec §8.2)."""
    interval_dir = f"{interval:g}"
    return f"{camera}/{interval_dir}/{sheet_filename(start, interval, count)}"


def sheet_url(camera: str, start: float, interval: float, count: int) -> str:
    return f"/v1/scrub/{camera}/sheet/{sheet_filename(start, interval, count)}"


def parse_sheet_spec(spec: str) -> tuple[float, float, int]:
    """Parse "{start}-{interval}-{count}.jpg" -> (start, interval, count).

    Raises ValueError on malformed input.
    """
    if not spec.endswith(".jpg") and not spec.endswith(".webp"):
        raise ValueError(f"unrecognised sheet extension: {spec}")
    stem = spec.rsplit(".", 1)[0]
    parts = stem.split("-")
    if len(parts) != 3:
        raise ValueError(f"malformed sheet spec: {spec}")
    start_s, interval_s, count_s = parts
    return float(start_s), float(interval_s), int(count_s)


@dataclass
class Frame:
    """A single sampled frame: its achieved wall-clock timestamp and source
    image path (already written to a temp location by the extractor)."""

    timestamp: float
    path: str


@dataclass
class Assignment:
    """One accepted cell placement within a bucket."""

    idx: int
    timestamp: float
    path: str


@dataclass
class AssignResult:
    bucket_start: float
    accepted: list[Assignment]
    # If the bucket had to be split (an off-grid frame or a gap), this is the
    # timestamp the *next* bucket should start at, and the remaining frames
    # that belong to it (not yet assigned -- caller re-invokes for them).
    split_at: float | None
    remaining: list[Frame]


def assign_cells(
    frames: list[Frame],
    bucket_start: float,
    interval: float,
    *,
    max_gap_factor: float = 1.5,
) -> AssignResult:
    """Assign frames to cells of a bucket starting at `bucket_start`, enforcing
    the interval/2 achieved-timestamp bound and splitting on violation or on a
    recording gap (spec §4.2, §5.2) -- never silently rounding, never a
    placeholder cell.
    """
    accepted: list[Assignment] = []
    prev_ts: float | None = None
    for i, frame in enumerate(frames):
        # Gap check: an oversized jump since the previous accepted frame means
        # the recording itself had a hole -- split rather than bridge it.
        if prev_ts is not None and (frame.timestamp - prev_ts) > interval * max_gap_factor:
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        idx = cell_index(frame.timestamp, bucket_start, interval)
        if idx < 0:
            # Should not happen for a well-formed caller; treat as unassignable
            # and start a fresh bucket here rather than guess.
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        if not within_bound(frame.timestamp, bucket_start, interval, idx):
            # Achieved timestamp drifted off the grid -- split instead of
            # rounding two different moments onto the same cell.
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        if accepted and accepted[-1].idx == idx:
            # Two frames landed on the same cell (shouldn't happen with sane
            # input, but never overwrite silently) -- split here too.
            return AssignResult(bucket_start, accepted, frame.timestamp, frames[i:])

        accepted.append(Assignment(idx=idx, timestamp=frame.timestamp, path=frame.path))
        prev_ts = frame.timestamp

    return AssignResult(bucket_start, accepted, None, [])
