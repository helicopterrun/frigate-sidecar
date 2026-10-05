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
        (["verified"], "Activity"),
    ],
)
def test_who_prefers_identities(identities: list[str], expected: str) -> None:
    # identities win over labels, however many labels there are.
    labels = ["person", "dog"] if expected != "Activity" else []
    assert who(labels, identities) == expected


def test_who_falls_back_to_labels_when_identities_are_noise() -> None:
    assert who(["person"], ["verified", "unknown"]) == "Person"


def test_who_brand_in_labels() -> None:
    assert who(["amazon"], []) == "Amazon"
    assert who(["person", "ups"], []) == "Person and UPS"


# --- places ----------------------------------------------------------------


def test_pretty_camera() -> None:
    assert pretty_camera("gate-face") == "Gate Face"
    assert pretty_camera("front_door_cam") == "Front Door Cam"


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
        "Amazon near Front Door"
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
