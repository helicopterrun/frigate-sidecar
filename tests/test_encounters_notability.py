"""encounters/notability.py: solar elevation, night, and the tag rules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from marcellus.config import EncountersSection, LocationSection, Settings
from marcellus.encounters import notability
from marcellus.encounters.notability import Stamp, is_night, solar_elevation_deg, stamp
from marcellus.push import ladder_policy, policy_settings

SEATTLE = (47.6, -122.3)
_PACIFIC = ZoneInfo("America/Los_Angeles")

ZONES = {"sidewalk": "street", "lawn": "yard", "porch": "doors", "bedroom": "private"}


def _pacific(month: int, day: int, hour: int) -> float:
    return datetime(2026, month, day, hour, 0, tzinfo=_PACIFIC).timestamp()


NOON = _pacific(6, 22, 12)  # daytime anywhere sensible


def _m(
    labels: tuple[str, ...] = ("person",),
    zones: tuple[str, ...] = ("sidewalk",),
    *,
    camera: str = "cam-a",
    start: float = NOON,
    end: float | None = None,
    sub_labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "camera": camera,
        "start_time": start,
        "end_time": start + 10.0 if end is None else end,
        "labels": list(labels),
        "zones": list(zones),
        "sub_labels": list(sub_labels),
    }


def _stamp(*members: dict[str, Any], linger_s: float = 60.0) -> Stamp:
    return stamp(
        list(members),
        zone_classes=ZONES,
        linger_s=linger_s,
        latitude=SEATTLE[0],
        longitude=SEATTLE[1],
    )


# ---- sun ------------------------------------------------------------------


def test_solar_elevation_equator_equinox_noon() -> None:
    ts = datetime(2026, 3, 20, 12, 7, tzinfo=timezone.utc).timestamp()
    assert 88.5 < solar_elevation_deg(ts, 0.0, 0.0) <= 90.0


def test_solar_elevation_seattle_solstice_noons() -> None:
    june = datetime(2026, 6, 21, 20, 10, tzinfo=timezone.utc).timestamp()
    dec = datetime(2026, 12, 21, 20, 7, tzinfo=timezone.utc).timestamp()
    assert solar_elevation_deg(june, *SEATTLE) == pytest.approx(65.8, abs=0.6)
    assert solar_elevation_deg(dec, *SEATTLE) == pytest.approx(19.0, abs=0.6)


def test_solar_elevation_midnight_is_negative_and_symmetric_in_hemisphere() -> None:
    ts = datetime(2026, 3, 20, 0, 0, tzinfo=timezone.utc).timestamp()  # sun far side
    assert solar_elevation_deg(ts, 0.0, 0.0) < -80.0


@pytest.mark.parametrize("month,day", [(6, 22), (12, 22)])
def test_is_night_seattle_midnight_vs_noon(month: int, day: int) -> None:
    assert is_night(_pacific(month, day, 0), *SEATTLE) is True
    assert is_night(_pacific(month, day, 12), *SEATTLE) is False


def test_is_night_fallback_hours_use_local_clock() -> None:
    def at(h: int, m: int = 0) -> float:
        return datetime(2026, 1, 15, h, m).timestamp()  # naive = server-local

    assert is_night(at(23), None, None)
    assert is_night(at(22), None, None)
    assert not is_night(at(21, 59), None, None)
    assert is_night(at(5, 59), None, None)
    assert not is_night(at(6), None, None)
    assert not is_night(at(12), None, None)
    # One coordinate alone is not a location.
    assert is_night(at(23), 47.6, None)


# ---- rules, in order -----------------------------------------------------


def test_empty_encounter() -> None:
    assert stamp([], zone_classes={}, linger_s=60.0, latitude=None, longitude=None) == Stamp(
        "notable", "street", "off", "empty"
    )


def test_passer_by_is_background() -> None:
    s = _stamp(_m())
    assert s == Stamp("background", "street", "log", "passer_by")


def test_rule1_multi_camera_wins_over_everything() -> None:
    s = _stamp(
        _m(zones=("bedroom",), sub_labels=("chris",)),
        _m(camera="cam-b", labels=("raccoon",)),
    )
    assert (s.tag, s.reason) == ("notable", "multi_camera")
    assert s.place == "private"  # the stamp still records where it got to


def test_rule2_left_public_beats_alerted() -> None:
    s = _stamp(_m(zones=("porch",)))
    assert (s.tag, s.reason, s.place) == ("notable", "left_public", "doors")
    assert s.outcome == "notify"


def test_no_zone_member_counts_as_street() -> None:
    s = _stamp(_m(zones=()))
    assert (s.place, s.tag, s.reason) == ("street", "background", "passer_by")


def test_most_private_place_across_members() -> None:
    s = _stamp(_m(zones=("sidewalk",)), _m(zones=("lawn",)), _m(zones=("sidewalk", "lawn")))
    assert s.place == "yard"


def test_rule3_alerted_via_zone_override() -> None:
    ladder_policy.set_zone_overrides({"sidewalk": {"person": "urgent"}})
    s = _stamp(_m())
    assert (s.tag, s.reason, s.outcome, s.place) == ("notable", "alerted", "alarm", "street")


def test_zone_override_makes_outcome_louder_than_table() -> None:
    quiet = _stamp(_m(zones=("lawn",), sub_labels=()))
    assert quiet.outcome == "glance"
    ladder_policy.set_zone_overrides({"lawn": {"person": "notify"}})
    assert _stamp(_m(zones=("lawn",))).outcome == "notify"


def test_suppressed_maps_to_off() -> None:
    ladder_policy.set_off_cells({("person", "street")})
    s = _stamp(_m())
    assert s.outcome == "off"
    assert (s.tag, s.reason) == ("background", "passer_by")


def test_outcome_is_loudest_member_not_first() -> None:
    s = _stamp(_m(zones=("sidewalk",)), _m(zones=("lawn",)))
    assert s.outcome == "glance"


def test_rule4_recognised() -> None:
    s = _stamp(_m(sub_labels=("chris",)))
    assert (s.tag, s.reason) == ("notable", "recognised")
    assert _stamp(_m(sub_labels=("",))).tag == "background"


def test_rule4_before_animal_and_lingered() -> None:
    s = _stamp(_m(labels=("cat",), sub_labels=("mittens",), end=NOON + 600))
    assert s.reason == "recognised"


def test_rule5_animal() -> None:
    assert _stamp(_m(labels=("cat",))).reason == "animal"
    assert _stamp(_m(labels=("deer",))).reason == "animal"
    # dangerous animals are animals too (ladder reclassifies them; still log on the street)
    assert _stamp(_m(labels=("raccoon",))).reason == "animal"


def test_dog_walker_is_background() -> None:
    s = _stamp(_m(labels=("person", "dog")))
    assert (s.tag, s.reason) == ("background", "passer_by")


def test_dog_walker_across_members() -> None:
    s = _stamp(_m(labels=("person",)), _m(labels=("dog",), start=NOON + 5))
    assert (s.tag, s.reason) == ("background", "passer_by")


def test_dog_alone_is_notable() -> None:
    assert _stamp(_m(labels=("dog",))).reason == "animal"


def test_person_with_cat_is_notable() -> None:
    assert _stamp(_m(labels=("person", "cat"))).reason == "animal"


def test_dog_label_contributes_no_outcome_for_dog_walker() -> None:
    ladder_policy.set_table(
        {
            **ladder_policy.TABLE,
            "animal": {**ladder_policy.TABLE["animal"], "yard": "urgent"},
        }
    )
    assert _stamp(_m(labels=("dog",), zones=("lawn",))).outcome == "alarm"
    walker = _stamp(_m(labels=("person", "dog"), zones=("lawn",)))
    assert walker.outcome == "glance"  # the person's yard cell, not the dog's


def test_rule6_linger_boundary() -> None:
    assert _stamp(_m(end=NOON + 59.9)).tag == "background"
    s = _stamp(_m(end=NOON + 60.0))
    assert (s.tag, s.reason) == ("notable", "lingered")
    assert _stamp(_m(end=NOON + 60.0), linger_s=61.0).tag == "background"


def test_rule6_open_member_has_no_duration() -> None:
    open_member = _m(start=NOON + 70)
    open_member["end_time"] = None
    # Still-open members count as 0 s: the encounter's 70 s+ span is irrelevant.
    s = _stamp(_m(), open_member)
    assert (s.tag, s.reason) == ("background", "passer_by")


def test_rule6_chained_passers_by_are_not_lingering() -> None:
    # Three 10 s sightings spread over five minutes on one camera: a busy
    # sidewalk chained into one encounter, nobody stayed.
    s = _stamp(
        _m(start=NOON, end=NOON + 10),
        _m(start=NOON + 150, end=NOON + 160),
        _m(start=NOON + 290, end=NOON + 300),
    )
    assert (s.tag, s.reason) == ("background", "passer_by")


def test_rule6_one_long_member_is_lingering() -> None:
    s = _stamp(_m(start=NOON, end=NOON + 10), _m(start=NOON + 100, end=NOON + 170))
    assert (s.tag, s.reason) == ("notable", "lingered")


def test_rule7_night_by_location() -> None:
    s = _stamp(_m(start=_pacific(12, 22, 0)))
    assert (s.tag, s.reason) == ("notable", "night")
    assert _stamp(_m(start=_pacific(12, 22, 12))).tag == "background"


def test_rule7_night_fallback_hours() -> None:
    late = datetime(2026, 1, 15, 23, 30).timestamp()
    s = stamp(
        [_m(start=late)], zone_classes=ZONES, linger_s=60.0, latitude=None, longitude=None
    )
    assert (s.tag, s.reason) == ("notable", "night")


def test_members_accept_raw_json_columns() -> None:
    raw = {
        "camera": "cam-a",
        "start_time": NOON,
        "end_time": NOON + 5,
        "labels_json": '["person", "dog"]',
        "zones_json": '["lawn"]',
        "sub_labels_json": "[]",
    }
    s = _stamp(raw)
    assert (s.place, s.reason) == ("yard", "left_public")


def test_stamper_for_reads_zone_classes_at_call_time() -> None:
    settings = Settings(location=LocationSection(latitude=47.6, longitude=-122.3))
    stamper = notability.stamper_for(settings)
    member = _m(zones=("zz_unlisted",))
    before = stamper([member])
    active = {**policy_settings.default_settings(), "zone_classes": {"zz_unlisted": "private"}}
    policy_settings._active = active
    after = stamper([member])
    assert before.place != "private"
    assert (after.place, after.reason) == ("private", "left_public")


def test_stamper_for_uses_configured_linger() -> None:
    settings = Settings(
        encounters=EncountersSection(linger_s=5.0),
        location=LocationSection(latitude=47.6, longitude=-122.3),
    )
    s = notability.stamper_for(settings)([_m(end=NOON + 6)])
    assert s.reason == "lingered"


def test_stamp_values_are_in_policy_vocabularies() -> None:
    s = _stamp(_m(zones=("bedroom",)))
    assert s.place in policy_settings.PLACES
    assert s.outcome in policy_settings.OUTCOMES


@pytest.mark.parametrize(
    "kwargs",
    [
        {"latitude": 91, "longitude": 0},
        {"latitude": -91, "longitude": 0},
        {"latitude": 0, "longitude": 181},
        {"latitude": 0, "longitude": float("nan")},
        {"latitude": 47.6},  # both or neither
        {"longitude": -122.3},
    ],
)
def test_location_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        LocationSection(**kwargs)


def test_location_label_needs_no_coordinates() -> None:
    assert LocationSection(label="home").latitude is None


# ---- a place for cameras (zoneless members) ---------------------------------


def _stamp_cams(*members: dict[str, Any], camera_classes: dict[str, str]) -> Stamp:
    return stamp(
        list(members),
        zone_classes=ZONES,
        camera_classes=camera_classes,
        linger_s=60.0,
        latitude=SEATTLE[0],
        longitude=SEATTLE[1],
    )


def test_zoneless_member_takes_its_cameras_place() -> None:
    s = _stamp_cams(_m(zones=()), camera_classes={"cam-a": "doors"})
    assert (s.place, s.tag, s.reason) == ("doors", "notable", "left_public")
    # Evaluated at that place, exactly like a member seen in a doors zone.
    assert s.outcome == _stamp(_m(zones=("porch",))).outcome == "notify"


def test_zoneless_member_without_an_entry_uses_the_name_guess() -> None:
    assert _stamp_cams(_m(zones=(), camera="front-door"), camera_classes={}).place == "doors"
    # No hint in the name: falls back to Semi-private (yard), not Public.
    s = _stamp_cams(_m(zones=()), camera_classes={})
    assert (s.place, s.reason) == ("yard", "left_public")


def test_camera_place_street_keeps_a_zoneless_member_a_passer_by() -> None:
    s = _stamp_cams(_m(zones=()), camera_classes={"cam-a": "street"})
    assert s == Stamp("background", "street", "log", "passer_by")


def test_camera_place_ignored_when_the_member_has_zones() -> None:
    s = _stamp_cams(_m(zones=("sidewalk",)), camera_classes={"cam-a": "private"})
    assert s == Stamp("background", "street", "log", "passer_by")


def test_camera_place_joins_the_most_private_place_across_members() -> None:
    s = _stamp_cams(
        _m(zones=("lawn",)),
        _m(zones=(), start=NOON + 5),
        camera_classes={"cam-a": "private"},
    )
    assert s.place == "private"


def test_no_camera_classes_means_zoneless_is_public() -> None:
    assert _stamp(_m(zones=())).place == "street"  # stamp() default: None


def test_stamper_for_reads_camera_classes_at_call_time() -> None:
    settings = Settings(location=LocationSection(latitude=47.6, longitude=-122.3))
    stamper = notability.stamper_for(settings)
    member = _m(zones=(), camera="cam-q")
    assert stamper([member]).place == "yard"  # name guess
    policy_settings._active = {
        **policy_settings.default_settings(),
        "camera_classes": {"cam-q": "street"},
    }
    assert stamper([member]).place == "street"
