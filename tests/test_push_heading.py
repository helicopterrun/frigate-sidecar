"""Calibrated heading from the path trail (`delivery_wire._heading_label`).

Measured 2026-08-15: this install's Frigate reports velocity_angle=0 and
speed=0 on every event (no zone distance calibration), so heading derives
from path_data dotted against the per-camera vector drawn on /cameras.
"""

from __future__ import annotations

import pytest

from frigate_sidecar.push import policy_settings
from frigate_sidecar.push.delivery_wire import (
    _build_motion,
    _heading_label,
    _movement_vector,
)


@pytest.fixture(autouse=True)
def _reset_policy():
    policy_settings.apply_settings(policy_settings.default_settings())
    yield
    policy_settings.apply_settings(policy_settings.default_settings())


def _calibrate(camera: str, dx: float, dy: float) -> None:
    settings = policy_settings.default_settings()
    settings["camera_headings"] = {camera: {"dx": dx, "dy": dy}}
    policy_settings.apply_settings(settings)


def test_movement_vector_none_for_short_or_jittery_trails():
    assert _movement_vector(None) is None
    assert _movement_vector([(0.5, 0.5)]) is None
    # All points within the jitter floor.
    assert _movement_vector([(0.500, 0.500), (0.505, 0.502), (0.501, 0.499)]) is None


def test_movement_vector_walks_back_past_jitter():
    # Recent jitter, but real travel further back in the trail.
    vec = _movement_vector([(0.2, 0.5), (0.5, 0.5), (0.505, 0.5)])
    assert vec is not None
    dx, dy = vec
    assert dx == pytest.approx(1.0, abs=0.05)
    assert dy == pytest.approx(0.0, abs=0.05)


def test_stationary_wins_regardless_of_calibration():
    assert _heading_label([(0.1, 0.1), (0.9, 0.9)], True, "garden") == "stationary"


def test_uncalibrated_camera_has_no_heading():
    assert _heading_label([(0.1, 0.5), (0.9, 0.5)], False, "garden") is None


def test_calibrated_headings():
    # "Home" is toward the top of the frame (dy = -1).
    _calibrate("garden", 0.0, -1.0)
    # Moving up the frame: approaching.
    assert _heading_label([(0.5, 0.9), (0.5, 0.4)], False, "garden") == "approaching"
    # Moving down the frame: leaving.
    assert _heading_label([(0.5, 0.2), (0.5, 0.8)], False, "garden") == "leaving"
    # Moving sideways: passing.
    assert _heading_label([(0.1, 0.5), (0.9, 0.5)], False, "garden") == "passing"
    # A different camera stays uncalibrated.
    assert _heading_label([(0.5, 0.9), (0.5, 0.4)], False, "street") is None


def test_build_motion_shapes():
    _calibrate("garden", 1.0, 0.0)
    assert _build_motion([(0.1, 0.5), (0.9, 0.5)], False, "garden") == {
        "heading": "approaching"
    }
    assert _build_motion(None, False, "garden") is None
    assert _build_motion(None, True, "garden") == {"heading": "stationary"}
