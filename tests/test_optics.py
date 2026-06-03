"""Pins the placement-planner optics math (mirrored in static/js/placement.js)."""

from __future__ import annotations

import math

import pytest

from frigate_sidecar.analysis import optics


def test_fit_varifocal_passes_through_both_anchors() -> None:
    anchors = [(2.7, 114.0), (12.0, 47.0)]
    w, f0 = optics.fit_varifocal(anchors)
    for focal, hfov in anchors:
        assert optics.hfov_from_focal(focal, w, f0) == pytest.approx(hfov, abs=1e-6)


def test_hfov_monotonic_in_focal() -> None:
    w, f0 = optics.fit_varifocal([(2.7, 114.0), (12.0, 47.0)])
    prev = 999.0
    for focal in [2.7, 4, 6, 8, 10, 12]:
        hfov = optics.hfov_from_focal(focal, w, f0)
        assert hfov < prev  # zooming in narrows the field
        prev = hfov


def test_focal_from_hfov_is_inverse() -> None:
    w, f0 = optics.fit_varifocal([(2.7, 114.0), (12.0, 47.0)])
    for focal in [3.0, 5.5, 9.1]:
        hfov = optics.hfov_from_focal(focal, w, f0)
        assert optics.focal_from_hfov(hfov, w, f0) == pytest.approx(focal, abs=1e-6)


def test_max_distance_and_object_px_are_consistent() -> None:
    # At exactly max_distance for a target, the object's on-screen width == target.
    width_ft, det_w, hfov, target = 0.5, 1280, 80.0, 80.0
    d = optics.max_distance_ft(width_ft, det_w, hfov, target)
    assert optics.object_px_width(width_ft, det_w, hfov, d) == pytest.approx(target, abs=1e-6)


def test_main_stream_doubles_reach_vs_substream() -> None:
    # 2688 main is ~2.1x the 1280 substream -> proportionally farther reach.
    sub = optics.max_distance_ft(0.5, 1280, 90.0, 80.0)
    main = optics.max_distance_ft(0.5, 2688, 90.0, 80.0)
    assert main / sub == pytest.approx(2688 / 1280, abs=1e-6)


def test_px_per_ft_falls_with_distance() -> None:
    near = optics.px_per_ft(1280, 90.0, 5)
    far = optics.px_per_ft(1280, 90.0, 50)
    assert near == pytest.approx(far * 10, abs=1e-6)


def test_target_area_matches_width_squared_times_aspect() -> None:
    assert optics.target_area_px2(80, 1.4) == 80 * 80 * 1.4


def test_known_value_hfov_90deg() -> None:
    # A sensor exactly as wide as 2*focal gives a 90 deg HFOV.
    assert optics.hfov_from_focal(4.0, 8.0) == pytest.approx(90.0, abs=1e-9)
    assert optics.hfov_from_focal(4.0, 8.0) == math.degrees(2 * math.atan(1.0))


def test_presets_payload_shape() -> None:
    p = optics.presets_payload()
    assert {"lenses", "resolutions", "objects", "cameras"} <= p.keys()

    # Every varifocal lens carries a fitted sensor width + offset for the JS.
    vari = [lo for lo in p["lenses"] if lo["type"] == "varifocal"]
    assert vari
    for lo in vari:
        assert "sensor_width_mm" in lo and "focal_offset_mm" in lo

    for obj in p["objects"]:
        assert {"id", "label", "width_ft", "aspect", "target_px"} <= obj.keys()

    # Deployed cameras reference a real lens + resolution id.
    lens_ids = {lo["id"] for lo in p["lenses"]}
    res_ids = {r["id"] for r in p["resolutions"]}
    for cam in p["cameras"]:
        assert cam["lens"] in lens_ids
        assert cam["res"] in res_ids
