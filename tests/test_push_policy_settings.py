"""Unit tests for `push/policy_settings.py` (Elsinore Phase 4): defaults,
validation, the zone-name guessing heuristic, persistence, and the wiring
that applies a settings document to the live routing engine.
"""

from __future__ import annotations

from pathlib import Path

from frigate_sidecar.push import ladder_policy, policy_settings


def test_default_routing_table_matches_the_brief_exactly():
    assert policy_settings.default_settings()["routing_table"] == {
        "stranger": {
            "street": "log", "yard": "quiet", "doors": "notify",
            "private": "notify", "off_limits": "urgent",
        },
        "known": {
            "street": "log", "yard": "log", "doors": "quiet",
            "private": "quiet", "off_limits": "quiet",
        },
        "animal": {
            "street": "log", "yard": "quiet", "doors": "quiet",
            "private": "quiet", "off_limits": "quiet",
        },
        "thing": {
            "street": "log", "yard": "log", "doors": "log",
            "private": "log", "off_limits": "quiet",
        },
    }


def test_default_settings_shape():
    settings = policy_settings.default_settings()
    assert settings["v"] == 1
    assert settings["zone_classes"] == {}
    assert settings["zone_overrides"] == {}
    assert settings["live_activities"] == {
        "package": True, "bins": True, "openings": True, "person": True,
        "opening_picks": [],
    }


# -- validation ---------------------------------------------------------


def _valid() -> dict:
    return policy_settings.default_settings()


def test_valid_default_settings_pass_validation():
    assert policy_settings.validate_settings(_valid()) == []


def test_rejects_invalid_level():
    data = _valid()
    data["routing_table"]["stranger"]["doors"] = "screaming"
    errors = policy_settings.validate_settings(data)
    assert any("routing_table.stranger.doors" in e for e in errors)


def test_rejects_unknown_subject():
    data = _valid()
    data["routing_table"]["ghost"] = {p: "log" for p in policy_settings.PLACES}
    errors = policy_settings.validate_settings(data)
    assert any("unknown subject" in e for e in errors)


def test_rejects_missing_place_in_routing_table():
    data = _valid()
    del data["routing_table"]["stranger"]["doors"]
    errors = policy_settings.validate_settings(data)
    assert any("routing_table.stranger.doors" in e for e in errors)


def test_rejects_invalid_zone_class():
    data = _valid()
    data["zone_classes"] = {"driveway": "not_a_place"}
    errors = policy_settings.validate_settings(data)
    assert any("zone_classes.driveway" in e for e in errors)


def test_rejects_invalid_subject_in_zone_overrides():
    data = _valid()
    data["zone_overrides"] = {"driveway": {"ghost_subject": "urgent"}}
    errors = policy_settings.validate_settings(data)
    assert any("zone_overrides.driveway" in e and "ghost_subject" in e for e in errors)


def test_rejects_invalid_level_in_zone_overrides():
    data = _valid()
    data["zone_overrides"] = {"driveway": {"thing": "screaming"}}
    errors = policy_settings.validate_settings(data)
    assert any("zone_overrides.driveway.thing" in e for e in errors)


def test_zone_overrides_allow_unknown_zone_names():
    # The user may configure a zone before it appears in Frigate.
    data = _valid()
    data["zone_overrides"] = {"not_yet_a_real_zone": {"thing": "urgent"}}
    assert policy_settings.validate_settings(data) == []


def test_rejects_unknown_live_activity_family():
    data = _valid()
    data["live_activities"]["robots"] = True
    errors = policy_settings.validate_settings(data)
    assert any("unknown key" in e for e in errors)


def test_rejects_non_bool_family_toggle():
    data = _valid()
    data["live_activities"]["package"] = "yes"
    errors = policy_settings.validate_settings(data)
    assert any("live_activities.package" in e for e in errors)


def test_unknown_top_level_field_is_ignored():
    data = _valid()
    data["some_future_field"] = {"whatever": True}
    assert policy_settings.validate_settings(data) == []


# -- zone-name guessing heuristic ----------------------------------------


def test_guess_street_patterns():
    for name in ("nw_49th_street", "county_road", "sidewalk", "curbside", "highway_view"):
        assert policy_settings.guess_zone_class(name) == "street"


def test_guess_yard_patterns():
    for name in ("driveway", "front_porch", "garden_path", "front_yard", "parking_lot"):
        assert policy_settings.guess_zone_class(name) == "yard"


def test_guess_doors_patterns():
    for name in ("front_door", "side_gate", "main_entry", "back_entrance", "kitchen_window"):
        assert policy_settings.guess_zone_class(name) == "doors"


def test_guess_private_patterns():
    for name in ("backyard", "side_yard", "rear_lot", "the_alley", "fence_line"):
        assert policy_settings.guess_zone_class(name) == "private"


def test_guess_street_pattern_wins_over_the_coincidental_private_substring():
    # "sidewalk" is a literal street pattern; it also happens to contain
    # "side" (a private pattern) as a substring -- street must win.
    assert policy_settings.guess_zone_class("sidewalk") == "street"


def test_guess_off_limits_patterns():
    for name in ("pool_area", "garden_shed", "equipment_room", "restricted_zone"):
        assert policy_settings.guess_zone_class(name) == "off_limits"


def test_guess_defaults_to_yard_for_unrecognized_name():
    assert policy_settings.guess_zone_class("zone_47") == "yard"


def test_guess_prefers_specific_pattern_over_broad_one():
    # Contains both a yard hint ("front") and a doors hint ("entry") --
    # doors wins (design doc's own example).
    assert policy_settings.guess_zone_class("front_entry_person") == "doors"


def test_guess_falls_back_to_camera_name():
    assert policy_settings.guess_zone_class("nw_49th_st", cameras=("street",)) == "street"
    assert policy_settings.guess_zone_class("zone_47", cameras=("driveway",)) == "yard"


# -- persistence ----------------------------------------------------------


def test_load_settings_returns_defaults_when_file_absent(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    assert policy_settings.load_settings(path) == policy_settings.default_settings()


def test_load_settings_returns_defaults_on_corrupt_json(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    path.write_text("{not json")
    assert policy_settings.load_settings(path) == policy_settings.default_settings()


def test_save_then_load_round_trips(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    data = policy_settings.default_settings()
    data["zone_classes"]["driveway"] = "yard"
    data["routing_table"]["thing"]["doors"] = "quiet"
    policy_settings.save_settings(path, data)

    loaded = policy_settings.load_settings(path)
    assert loaded["zone_classes"] == {"driveway": "yard"}
    assert loaded["routing_table"]["thing"]["doors"] == "quiet"


def test_load_settings_fills_in_missing_fields_from_an_older_partial_file(tmp_path: Path):
    path = tmp_path / "push_settings.json"
    path.write_text('{"v": 1, "zone_classes": {"driveway": "yard"}}')
    loaded = policy_settings.load_settings(path)
    assert loaded["routing_table"] == policy_settings.DEFAULT_ROUTING_TABLE
    assert loaded["zone_classes"] == {"driveway": "yard"}
    assert loaded["zone_overrides"] == {}
    assert loaded["live_activities"]["package"] is True


def test_normalize_settings_keeps_valid_zone_overrides():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {"front_entry_person": {"thing": "notify"}}


def test_normalize_settings_drops_invalid_entries_within_a_zone_override():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {
        "driveway": {"animal": "log", "ghost": "urgent", "thing": "not_a_level"},
    }
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {"driveway": {"animal": "log"}}


def test_normalize_settings_removes_empty_inner_dicts():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {"driveway": {}}
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {}


def test_normalize_settings_removes_zone_left_empty_after_filtering():
    data = policy_settings.default_settings()
    data["zone_overrides"] = {"driveway": {"ghost": "urgent"}}
    normalized = policy_settings.normalize_settings(data)
    assert normalized["zone_overrides"] == {}


# -- applying to the routing engine ----------------------------------------


def test_apply_settings_changes_what_the_ladder_evaluates_against():
    from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder

    custom = policy_settings.default_settings()
    custom["routing_table"]["thing"]["yard"] = "urgent"
    policy_settings.apply_settings(custom)

    assert ladder_policy.TABLE["thing"]["yard"] == "urgent"
    level = evaluate_ladder(Snapshot(subject="thing", place="yard"))
    assert level == "urgent"


def test_apply_settings_copies_the_table_so_later_mutation_does_not_leak():
    custom = policy_settings.default_settings()
    policy_settings.apply_settings(custom)
    custom["routing_table"]["thing"]["yard"] = "urgent"
    assert ladder_policy.TABLE["thing"]["yard"] != "urgent"


def test_get_active_lazily_defaults():
    policy_settings.reset_for_tests()
    active = policy_settings.get_active()
    assert active == policy_settings.default_settings()


# -- zone overrides (Phase 4 addendum) -------------------------------------


def test_zone_override_present_bypasses_the_base_table():
    from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder

    settings = policy_settings.default_settings()
    settings["routing_table"]["thing"]["doors"] = "log"  # base table says log
    settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    policy_settings.apply_settings(settings)

    level = evaluate_ladder(
        Snapshot(subject="thing", place="doors", zone="front_entry_person")
    )
    assert level == "notify"  # the override, not the base table's "log"


def test_zone_override_absent_falls_through_to_the_base_table():
    from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder

    settings = policy_settings.default_settings()
    settings["zone_overrides"] = {"front_entry_person": {"thing": "notify"}}
    policy_settings.apply_settings(settings)

    # Different zone -- no override applies, ordinary table lookup runs.
    level = evaluate_ladder(Snapshot(subject="thing", place="doors", zone="side_door"))
    assert level == settings["routing_table"]["thing"]["doors"]

    # Same zone, different subject -- no override applies either.
    level = evaluate_ladder(
        Snapshot(subject="stranger", place="doors", zone="front_entry_person")
    )
    assert level == settings["routing_table"]["stranger"]["doors"]


def test_zone_override_does_not_affect_other_zones_in_the_same_place_class():
    from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder

    settings = policy_settings.default_settings()
    settings["zone_overrides"] = {"driveway": {"animal": "log"}}
    policy_settings.apply_settings(settings)

    assert evaluate_ladder(Snapshot(subject="animal", place="yard", zone="driveway")) == "log"
    # Another yard-classified zone, no override for it -- base table applies.
    assert evaluate_ladder(
        Snapshot(subject="animal", place="yard", zone="parking_spot")
    ) == settings["routing_table"]["animal"]["yard"]
