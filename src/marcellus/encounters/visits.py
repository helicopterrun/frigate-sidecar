"""Visits ("stops"): an encounter's members ordered by time, with consecutive
members on the same camera merged into one stop. Pure -- plain data in, plain
data out. The encounter title's route (`title.py`) and the `stops` array on
`EncounterSummary` both build on `order_members`/`zone_label`, so they cannot
disagree about member order or zone naming; they differ on purpose in how they
collapse (title: consecutive duplicate *place names*; stops: consecutive
*same camera*).

    driveway, door, driveway   -> 3 stops (a return visit is its own stop)
    driveway, driveway, door   -> 2 stops
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class MemberStop:
    """What route/visit building needs from one encounter member."""

    camera: str
    start: float
    end: float | None = None
    first_zone: str = ""
    zones: Sequence[str] = ()
    joined_at: float = 0.0


@dataclass(frozen=True)
class Stop:
    """One visit: `zone` is a display name (None when the first member had no
    zone); `end` is None while any member of the stop is still open."""

    camera: str
    zone: str | None
    start: float
    end: float | None


def order_members(members: Iterable[MemberStop]) -> list[MemberStop]:
    """By start time, then joined_at (stable beyond that)."""
    return sorted(members, key=lambda m: (m.start, m.joined_at))


def member_zone(member: MemberStop) -> str:
    """The raw zone key a member is placed at: `first_zone`, else its first
    zone, else ""."""
    return member.first_zone or (member.zones[0] if member.zones else "")


def humanise(text: str, separators: str) -> str:
    """Split on `separators`, capitalise only each word's first letter and
    leave the rest as written ("nw_49th_st" -> "Nw 49th St"; `str.title()`
    would give "Nw 49Th St")."""
    return " ".join(w[:1].upper() + w[1:] for w in re.split(f"[{separators}]+", text) if w)


def zone_label(zone: str, zone_display: Callable[[str], str | None]) -> str:
    """A zone's display name: the configured one, else the humanised key."""
    return zone_display(zone) or humanise(zone, "_")


def build_stops(
    members: Iterable[MemberStop], zone_display: Callable[[str], str | None]
) -> list[Stop]:
    """Merge consecutive same-camera members (in `order_members` order) into
    stops. `start` is the stop's earliest member start; `end` the latest member
    end, or None if any member is open; `zone` comes from the stop's first
    member."""
    stops: list[Stop] = []
    for m in order_members(members):
        if stops and stops[-1].camera == m.camera:
            prev = stops[-1]
            end = None if prev.end is None or m.end is None else max(prev.end, m.end)
            stops[-1] = Stop(prev.camera, prev.zone, min(prev.start, m.start), end)
            continue
        raw = member_zone(m)
        stops.append(
            Stop(m.camera, zone_label(raw, zone_display) if raw else None, m.start, m.end)
        )
    return stops
