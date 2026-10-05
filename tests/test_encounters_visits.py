"""encounters/visits.py: ordered stops (consecutive same-camera members merge)."""

from __future__ import annotations

from marcellus.encounters.visits import MemberStop, Stop, build_stops, order_members

ZONES = {"drive": "Driveway", "front_door": "Front Door"}


def _display(zone: str) -> str | None:
    return ZONES.get(zone)


def test_consecutive_same_camera_merges() -> None:
    members = [
        MemberStop("cam-a", 10.0, 20.0, first_zone="drive"),
        MemberStop("cam-a", 25.0, 40.0, first_zone="front_door"),
        MemberStop("cam-b", 50.0, 60.0),
    ]
    assert build_stops(members, _display) == [
        Stop("cam-a", "Driveway", 10.0, 40.0),  # zone from the stop's FIRST member
        Stop("cam-b", None, 50.0, 60.0),
    ]


def test_return_visit_is_not_merged() -> None:
    members = [
        MemberStop("drive-cam", 1.0, 2.0),
        MemberStop("door-cam", 3.0, 4.0),
        MemberStop("drive-cam", 5.0, 6.0),
    ]
    assert [s.camera for s in build_stops(members, _display)] == [
        "drive-cam",
        "door-cam",
        "drive-cam",
    ]


def test_open_member_makes_stop_end_null() -> None:
    members = [MemberStop("a", 1.0, 5.0), MemberStop("a", 6.0, None)]
    assert build_stops(members, _display) == [Stop("a", None, 1.0, None)]
    # An open member that is followed by a closed one still leaves the stop open.
    members = [MemberStop("a", 1.0, None), MemberStop("a", 6.0, 9.0)]
    assert build_stops(members, _display)[0].end is None


def test_stop_end_is_latest_member_end() -> None:
    members = [MemberStop("a", 1.0, 30.0), MemberStop("a", 5.0, 10.0)]
    assert build_stops(members, _display) == [Stop("a", None, 1.0, 30.0)]


def test_zone_resolution_and_null() -> None:
    members = [
        MemberStop("a", 1.0, 2.0, zones=["front_door", "drive"]),  # no first_zone -> zones[0]
        MemberStop("b", 3.0, 4.0, first_zone="nw_49th_st"),  # humanised fallback
        MemberStop("c", 5.0, 6.0),  # no zone
    ]
    assert [s.zone for s in build_stops(members, _display)] == [
        "Front Door",
        "Nw 49th St",
        None,
    ]


def test_camera_is_raw_key() -> None:
    assert build_stops([MemberStop("gate-face", 1.0, 2.0)], _display)[0].camera == "gate-face"


def test_ordering_by_start_then_joined_at() -> None:
    members = [
        MemberStop("late", 30.0, 31.0),
        MemberStop("b-joined-second", 10.0, 11.0, joined_at=2.0),
        MemberStop("a-joined-first", 10.0, 11.0, joined_at=1.0),
    ]
    assert [m.camera for m in order_members(members)] == [
        "a-joined-first",
        "b-joined-second",
        "late",
    ]
    assert [s.camera for s in build_stops(members, _display)] == [
        "a-joined-first",
        "b-joined-second",
        "late",
    ]


def test_no_members_no_stops() -> None:
    assert build_stops([], _display) == []


def test_no_cap_on_stops() -> None:
    members = [MemberStop("a" if i % 2 else "b", float(i), float(i) + 0.5) for i in range(30)]
    assert len(build_stops(members, _display)) == 30
