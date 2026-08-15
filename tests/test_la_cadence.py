"""§8 Live Activity cadence: delta-only pushes, ≤6 for a typical 60s visit.

Tests the delta detection function directly — no real-time waits needed.
"""

from __future__ import annotations

from frigate_sidecar.push.delivery_wire import _copy, _la_has_visible_delta


def _delta(**kw):
    defaults = dict(
        mutation="enrich", prev_mutation="enrich",
        level="notify", prev_level="notify",
        primary="Person at front door", prev_primary="Person at front door",
        glyph="figure.walk", prev_glyph="figure.walk",
        current_zones=("front_door",), prev_zones=("front_door",),
        path_len=5, prev_path_len=5,
        heading="approaching", prev_heading="approaching",
    )
    defaults.update(kw)
    return _la_has_visible_delta(**defaults)


def test_no_change_suppressed():
    assert _delta() is None


def test_create_always_pushes():
    assert _delta(mutation="create") == "create"


def test_escalate_always_pushes():
    assert _delta(mutation="escalate") == "escalate"


def test_deescalate_always_pushes():
    assert _delta(mutation="deescalate") == "deescalate"


def test_resolve_always_pushes():
    assert _delta(mutation="resolve") == "resolve"


def test_level_change_pushes():
    assert _delta(level="urgent", prev_level="notify") == "level_change"


def test_text_change_pushes():
    assert _delta(primary="Person at porch") == "text_change"


def test_glyph_change_pushes():
    assert _delta(glyph="figure.wave") == "glyph_change"


def test_zone_transition_pushes():
    assert _delta(current_zones=("porch",)) == "zone_transition"


def test_heading_change_pushes():
    assert _delta(heading="leaving") == "heading_change"


def test_path_growth_pushes():
    assert _delta(path_len=8, prev_path_len=5) == "path_growth"


def test_path_growth_below_threshold_suppressed():
    assert _delta(path_len=7, prev_path_len=5) is None


def test_heading_none_suppressed():
    assert _delta(heading=None, prev_heading=None) is None


def test_60s_visit_produces_at_most_6_pushes():
    """Simulate a 60s person visit: create, 4 zone-dwell enrich steps
    (no visible delta), 1 zone transition, 1 enrichment, 1 deescalate,
    1 resolve. Only steps with visible deltas push."""
    steps = [
        dict(mutation="create"),
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich", current_zones=("porch",), prev_zones=("front_door",)),
        dict(mutation="enrich"),  # no change (zones same as last push)
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich", primary="Alex at Porch", prev_primary="Person at front door"),
        dict(mutation="deescalate"),
        dict(mutation="enrich"),  # no change
        dict(mutation="enrich"),  # no change
        dict(mutation="resolve"),
    ]
    pushes = sum(1 for s in steps if _delta(**{**dict(
        prev_mutation="enrich", level="notify", prev_level="notify",
        primary="Person at front door", prev_primary="Person at front door",
        glyph="figure.walk", prev_glyph="figure.walk",
        current_zones=("front_door",), prev_zones=("front_door",),
        path_len=5, prev_path_len=5,
        heading="approaching", prev_heading="approaching",
    ), **s}) is not None)
    assert pushes <= 6, f"expected ≤6 LA pushes, got {pushes}"
    assert pushes >= 4  # create + zone_transition + text_change + deescalate + resolve = 5


# -- Copy text ----------------------------------------------------------------

def test_create_secondary_omits_elapsed_zero():
    # Secondary carries what the title doesn't: the camera, never a repeat
    # of the place ("Person at Front Garden / Front Garden", 2026-08-14).
    primary, secondary = _copy("stranger", "person", "doorbell", "front_garden", 0.0)
    assert primary == "Person at Front Garden"
    assert secondary == "Doorbell"
    assert "0s" not in secondary


def test_enrich_secondary_includes_elapsed():
    _, secondary = _copy("stranger", "person", "doorbell", "front_garden", 45.0)
    assert secondary == "Doorbell · 45s"


def test_copy_prefers_frigate_friendly_name():
    # "front_entry_person" is a rule name, not a place — Frigate's
    # friendly_name wins when configured (loaded at startup from config.yml).
    from frigate_sidecar.push import policy_settings
    policy_settings._zone_display_names = {"front_entry_person": "Front Walk"}
    try:
        primary, _ = _copy("stranger", "person", "garden", "front_entry_person", 0.0)
        assert primary == "Person at Front Walk"
    finally:
        policy_settings._zone_display_names = {}


def test_secondary_empty_when_title_already_used_camera():
    # No zone: the title falls back to the camera, so a camera-only
    # secondary would echo it — stay empty on create, elapsed-only later.
    _, secondary = _copy("stranger", "person", "doorbell", "", 0.0)
    assert secondary == ""
    _, secondary = _copy("stranger", "person", "doorbell", "", 30.0)
    assert secondary == "30s"
