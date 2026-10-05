"""encounters/title.py: pure "who + route" titles."""

from __future__ import annotations

import pytest

from marcellus.encounters.title import (
    MemberStop,
    Place,
    encounter_title,
    pretty_camera,
    route_places,
    who,
)

ZONE_NAMES = {
    "sidewalk": "Sidewalk",
    "front_garden": "Front Garden",
    "front_door": "Front Door",
    "drive": "Driveway",
}


def _display(zone: str) -> str | None:
    return ZONE_NAMES.get(zone)


def _zones(*names: str) -> list[Place]:
    return [Place(n, True) for n in names]


# --- who -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["person"], "Person"),
        (["dog", "person"], "Person and dog"),
        (["package", "dog", "person"], "Person, dog and package"),
        (["package", "car", "cat", "person"], "Person, cat, car and package"),
        (["truck", "dog"], "Dog and truck"),
        (["person", "person-verified"], "Person"),
        (["dog-verified"], "Dog"),
        (["waste_bin"], "Waste bin"),
        ([], "Activity"),
        (["license_plate"], "Activity"),
    ],
)
def test_who_from_labels(labels: list[str], expected: str) -> None:
    assert who(labels, []) == expected


def test_who_sub_label_qualified_label_normalises_to_base() -> None:
    assert who(["person-verified", "dog"], ["verified"]) == "Person and dog"


@pytest.mark.parametrize(
    ("identities", "expected"),
    [
        (["alice"], "Alice"),
        (["Alice", "Bob"], "Alice and Bob"),
        (["alice", "bob", "carol"], "Alice, Bob and Carol"),
        (["ABC123"], "ABC123"),
        (["amazon"], "Amazon"),
        (["ups"], "UPS"),
        (["fedex"], "FedEx"),
        (["USPS"], "USPS"),
        (["dhl"], "DHL"),
        (["amazon", "alice"], "Alice and Amazon"),
        (["alice", "Alice"], "Alice"),
    ],
)
def test_who_identity_words(identities: list[str], expected: str) -> None:
    assert who(["person"], identities) == expected


def test_who_name_replaces_only_person() -> None:
    assert who(["person", "dog"], ["Chris"]) == "Chris and dog"
    assert who(["person"], ["Chris"]) == "Chris"
    assert who(["person", "dog"], ["Alice", "Bob"]) == "Alice, Bob and dog"
    # A name does not replace vehicles or packages.
    assert who(["person", "car", "package"], ["Chris"]) == "Chris, car and package"


def test_who_brand_replaces_person_and_motor_vehicles() -> None:
    assert who(["car", "package"], ["amazon"]) == "Amazon and package"
    assert who(["person", "truck", "package"], ["ups"]) == "UPS and package"
    assert who(["person", "dog", "motorcycle"], ["fedex"]) == "FedEx and dog"
    # Bicycles are not replaced.
    assert who(["person", "bicycle"], ["dhl"]) == "DHL and bicycle"


def test_who_name_and_brand_together() -> None:
    assert who(["person", "car", "dog"], ["alice", "amazon"]) == "Alice, Amazon and dog"


def test_who_keeps_other_subjects_in_rank_order() -> None:
    assert who(["package", "waste_bin", "cat", "person"], ["Chris"]) == (
        "Chris, cat, package and waste bin"
    )


def test_who_junk_identities_fall_back_to_labels() -> None:
    assert who(["person", "dog"], ["verified"]) == "Person and dog"
    assert who(["person"], ["verified", "unknown"]) == "Person"
    assert who([], ["verified"]) == "Activity"


def test_who_skips_identities_that_repeat_own_labels_or_qualifiers() -> None:
    assert who(["person", "dog"], ["Person", "DOG"]) == "Person and dog"
    # "foo" is a qualifier of the label "person-foo": not a name.
    assert who(["person-foo"], ["foo"]) == "Person"
    assert who(["person-foo", "dog"], ["foo", "Chris"]) == "Chris and dog"


def test_who_brand_in_labels() -> None:
    assert who(["amazon"], []) == "Amazon"
    assert who(["person", "ups"], []) == "UPS"
    # The brand as qualifier AND identity is still one brand.
    assert who(["person-amazon", "package"], ["amazon"]) == "Amazon and package"


# --- places ----------------------------------------------------------------


def test_pretty_camera() -> None:
    assert pretty_camera("gate-face") == "Gate Face"
    assert pretty_camera("front_door_cam") == "Front Door Cam"
    assert pretty_camera("cam-2nd_floor") == "Cam 2nd Floor"  # not "2Nd"


def test_unconfigured_zone_keeps_inner_casing_and_digits() -> None:
    stops = [MemberStop("street", 1.0, first_zone="nw_49th_st")]
    assert route_places(stops, lambda z: None) == [Place("Nw 49th St", True)]


def test_route_places_zone_then_camera_fallback() -> None:
    stops = [
        MemberStop("gate-face", 10.0, first_zone="sidewalk"),
        MemberStop("yard", 20.0, zones=["front_garden", "drive"]),  # no first_zone -> zones[0]
        MemberStop("porch", 30.0),  # no zone -> camera
        MemberStop("tool_shed", 40.0, first_zone="back_path"),  # unconfigured zone, humanised
    ]
    assert route_places(stops, _display) == [
        Place("Sidewalk", True),
        Place("Front Garden", True),
        Place("Porch", False),
        Place("Back Path", True),
    ]


def test_route_places_orders_by_start_time() -> None:
    stops = [
        MemberStop("a", 30.0, first_zone="front_door"),
        MemberStop("b", 10.0, first_zone="sidewalk"),
        MemberStop("c", 20.0, first_zone="front_garden"),
    ]
    assert [p.name for p in route_places(stops, _display)] == [
        "Sidewalk",
        "Front Garden",
        "Front Door",
    ]


def test_route_places_collapses_consecutive_duplicates_only() -> None:
    stops = [
        MemberStop("a", 1.0, first_zone="sidewalk"),
        MemberStop("b", 2.0, first_zone="sidewalk"),
        MemberStop("c", 3.0, first_zone="front_door"),
        MemberStop("d", 4.0, first_zone="sidewalk"),  # not consecutive: stays
    ]
    assert [p.name for p in route_places(stops, _display)] == ["Sidewalk", "Front Door", "Sidewalk"]


def test_route_places_collapses_repeated_camera_fallback() -> None:
    stops = [MemberStop("gate-face", 1.0), MemberStop("gate-face", 2.0)]
    assert route_places(stops, _display) == [Place("Gate Face", False)]


# --- full titles -----------------------------------------------------------


def test_title_one_zone() -> None:
    assert encounter_title(["person"], [], _zones("Front Door")) == "Person near Front Door"


def test_title_one_camera_fallback() -> None:
    assert (
        encounter_title(["person"], [], [Place("Gate Face", False)])
        == "Person · Gate Face camera"
    )


def test_title_two_to_four_places() -> None:
    labels = ["person", "dog"]
    assert (
        encounter_title(labels, [], _zones("Sidewalk", "Front Garden"))
        == "Person and dog · Sidewalk → Front Garden"
    )
    assert (
        encounter_title(labels, [], _zones("Sidewalk", "Front Garden", "Front Door"))
        == "Person and dog · Sidewalk → Front Garden → Front Door"
    )
    assert (
        encounter_title(["person"], [], _zones("A", "B", "C", "D"))
        == "Person · A → B → C → D"
    )


def test_title_more_than_four_places_ellipsis() -> None:
    assert (
        encounter_title(["person"], [], _zones("A", "B", "C", "D", "E", "F"))
        == "Person · A → B → … → F"
    )


def test_title_mixed_zone_and_camera_route() -> None:
    places = [Place("Sidewalk", True), Place("Gate Face", False)]
    assert (
        encounter_title(["person"], [], places) == "Person · Sidewalk → Gate Face"
    )


def test_title_identity_with_route() -> None:
    assert (
        encounter_title(["person"], ["alice"], _zones("Sidewalk", "Front Door"))
        == "Alice · Sidewalk → Front Door"
    )


def test_title_brand() -> None:
    assert encounter_title(["person", "package"], ["amazon"], _zones("Front Door")) == (
        "Amazon and package near Front Door"
    )


def test_title_no_places_is_just_who() -> None:
    assert encounter_title(["person", "dog"], [], []) == "Person and dog"


def test_title_empty_everything() -> None:
    assert encounter_title([], [], []) == "Activity"
    assert encounter_title([], [], [Place("Gate Face", False)]) == (
        "Activity · Gate Face camera"
    )


def test_title_end_to_end_with_real_display_names() -> None:
    stops = [
        MemberStop("street-cam", 100.0, first_zone="sidewalk"),
        MemberStop("yard-cam", 130.0, first_zone="front_garden"),
        MemberStop("porch-cam", 160.0, first_zone="front_door"),
    ]
    assert (
        encounter_title(["dog", "person"], [], route_places(stops, _display))
        == "Person and dog · Sidewalk → Front Garden → Front Door"
    )
