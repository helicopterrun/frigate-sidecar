"""Tests for the pure cell-assignment / cadence-verification logic
(scrub/grid.py). This is the highest-value test in the suite per the client-
side review (docs/scrub-cache-and-proxy-spec.md §11): the cadence guarantee
is silent until two series collide on one cell.
"""

from __future__ import annotations

from frigate_sidecar.scrub import grid


def test_achieved_timestamp_within_bound_all_accepted() -> None:
    interval = 1.0
    start = 1000.0
    frames = [grid.Frame(timestamp=start + k * interval, path=f"f{k}.jpg") for k in range(10)]
    result = grid.assign_cells(frames, start, interval)
    assert result.split_at is None
    assert len(result.accepted) == 10
    for a in result.accepted:
        assert grid.within_bound(a.timestamp, start, interval, a.idx)


def test_achieved_timestamp_within_bound_uses_own_interval_not_hardcoded_1s() -> None:
    """Same guarantee as above, but at the aged tier's own coarser interval
    (5.0s, §5.5) -- the /2 bound must scale with `interval`, not be hardcoded
    to the recent tier's 1.0s (would wrongly reject valid aged-tier frames,
    or wrongly accept off-grid ones)."""
    interval = 5.0
    start = 2000.0
    frames = [grid.Frame(timestamp=start + k * interval, path=f"f{k}.jpg") for k in range(6)]
    result = grid.assign_cells(frames, start, interval)
    assert result.split_at is None
    assert len(result.accepted) == 6
    for a in result.accepted:
        assert grid.within_bound(a.timestamp, start, interval, a.idx)
        # A 1.0s-scaled bound would be too tight for these 5.0s-spaced
        # frames were the check hardcoded -- pin the actual math directly.
        assert abs(a.timestamp - grid.grid_point(start, interval, a.idx)) <= interval / 2

    # A frame drifted by 2.5s (exactly interval/2 at 5.0s) is still within
    # bound; one micro-epsilon further out is not -- proves the bound tracks
    # `interval`, not a fixed 1.0s/0.5s constant.
    edge = grid.Frame(timestamp=start + 6 * interval + interval / 2, path="edge.jpg")
    assert grid.within_bound(edge.timestamp, start, interval, 6)
    too_far = grid.Frame(timestamp=start + 6 * interval + interval / 2 + 0.1, path="far.jpg")
    assert not grid.within_bound(too_far.timestamp, start, interval, 6)


def test_drift_off_grid_splits_bucket() -> None:
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + 0.0, path="f0.jpg"),
        grid.Frame(timestamp=start + 1.0, path="f1.jpg"),
        # Drifted more than interval/2 off the grid point for idx=2 (1002.0):
        # never silently round two different moments onto the same cell.
        grid.Frame(timestamp=start + 2.6, path="f2.jpg"),
        grid.Frame(timestamp=start + 3.6, path="f3.jpg"),
    ]
    result = grid.assign_cells(frames, start, interval)
    assert len(result.accepted) == 2
    assert result.split_at == start + 2.6
    assert [f.path for f in result.remaining] == ["f2.jpg", "f3.jpg"]

    # Re-invoking on the remaining frames with a fresh bucket start succeeds.
    result2 = grid.assign_cells(result.remaining, result.split_at, interval)
    assert result2.split_at is None
    assert len(result2.accepted) == 2


def test_recording_gap_splits_bucket_rather_than_bridging() -> None:
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start + 0.0, path="f0.jpg"),
        grid.Frame(timestamp=start + 1.0, path="f1.jpg"),
        # A 30s gap (recording hole) -- must split, never fabricate frames
        # for the missing span (no placeholder/black cell, spec §4.2).
        grid.Frame(timestamp=start + 31.0, path="f2.jpg"),
    ]
    result = grid.assign_cells(frames, start, interval)
    assert len(result.accepted) == 2
    assert result.split_at == start + 31.0
    assert len(result.remaining) == 1


def test_no_placeholder_cells_ever_emitted() -> None:
    """A gap never produces an accepted cell for the missing span -- only
    real frames appear in `accepted`."""
    interval = 1.0
    start = 1000.0
    frames = [
        grid.Frame(timestamp=start, path="f0.jpg"),
        grid.Frame(timestamp=start + 50.0, path="f1.jpg"),
    ]
    result = grid.assign_cells(frames, start, interval)
    assert len(result.accepted) == 1  # not 51 -- no fabricated in-between cells
    assert result.accepted[0].idx == 0


def test_sheet_url_immutable_across_growing_count() -> None:
    url_a = grid.sheet_url("doorbell", 1785380400, 1.0, 12)
    url_b = grid.sheet_url("doorbell", 1785380400, 1.0, 40)
    assert url_a != url_b
    assert url_a == "/v1/scrub/doorbell/sheet/1785380400-1.0-12.jpg"
    assert url_b == "/v1/scrub/doorbell/sheet/1785380400-1.0-40.jpg"


def test_sheet_spec_roundtrip() -> None:
    start, interval, count = grid.parse_sheet_spec("1785380400-1.0-96.jpg")
    assert (start, interval, count) == (1785380400.0, 1.0, 96)
    assert grid.sheet_filename(start, interval, count) == "1785380400-1.0-96.jpg"


def test_cell_index_row_major() -> None:
    cols = 12
    idx = grid.cell_index(1005.0, 1000.0, 1.0)  # 5th cell
    row, col = divmod(idx, cols)
    assert (row, col) == (0, 5)
    idx2 = grid.cell_index(1013.0, 1000.0, 1.0)
    row2, col2 = divmod(idx2, cols)
    assert (row2, col2) == (1, 1)
