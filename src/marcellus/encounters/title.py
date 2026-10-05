"""Human titles for encounters ("who + route"). Pure: plain data in, a string
out -- no DB, no network, no config -- so the whole rule set is unit-testable
and the routes compute a title at read time (nothing is stored).

    Person and dog · Sidewalk → Front Garden → Front Door
    Amazon near Front Door
    Person · Gate Face camera
    Activity

**Who** is the encounter's recognised identities when it has any (names, plate
text, delivery brands), otherwise the subject words from its labels. Subjects
read in a fixed order -- people, animals, vehicles, packages, then anything
else (bins, doors) -- and are joined "A", "A and B", "A, B and C".

**Route** is the encounter's members in time order, each reduced to one place
(its first zone's display name, else the camera) with consecutive duplicates
collapsed. One place reads like a push title ("{Who} near {Zone}" /
"{Who} · {Camera} camera"); two to four are listed with arrows; more than four
keep the first two, an ellipsis and the last.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from marcellus.encounters.linker import LABEL_FAMILIES, normalise_labels
from marcellus.push.live_activities import BIN_LABELS, OPENING_LABELS

#: Delivery brands, as Frigate classification sub_labels (lower-case) -> how
#: the brand is written.
BRANDS: dict[str, str] = {
    "amazon": "Amazon",
    "ups": "UPS",
    "fedex": "FedEx",
    "usps": "USPS",
    "dhl": "DHL",
}

#: Strings that show up in an encounter's `identities` without being one:
#: Frigate lists a recognised object as `person-verified` in `objects`, and
#: the encounter store keeps the `-verified` qualifier as a "sub_label".
_NOT_IDENTITIES = frozenset({"verified", "unknown"})

#: Subject order: people, animals, vehicles, packages, everything else.
_RANK_PERSON, _RANK_ANIMAL, _RANK_VEHICLE, _RANK_PACKAGE, _RANK_OTHER = range(5)

_FAMILY_RANK = {
    "person": _RANK_PERSON,
    "animal": _RANK_ANIMAL,
    "vehicle": _RANK_VEHICLE,
    "package": _RANK_PACKAGE,
}
_LABEL_RANK: dict[str, int] = {
    label: _FAMILY_RANK[family] for family, labels in LABEL_FAMILIES.items() for label in labels
}
for _label in BIN_LABELS | OPENING_LABELS:
    _LABEL_RANK.setdefault(_label, _RANK_OTHER)

_ELLIPSIS = "…"
_ARROW = " → "
_DOT = " · "
_MAX_LISTED_PLACES = 4


@dataclass(frozen=True)
class Place:
    """One stop on the route: a zone's display name, or (when the member had
    no zone) a prettified camera name."""

    name: str
    is_zone: bool


@dataclass(frozen=True)
class MemberStop:
    """What the route needs from one encounter member."""

    camera: str
    start: float
    first_zone: str = ""
    zones: Sequence[str] = ()


def pretty_camera(camera: str) -> str:
    """"gate-face" / "gate_face" -> "Gate Face" (same as the push/doorbell copy)."""
    return camera.replace("_", " ").replace("-", " ").title()


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _subject_words(labels: Iterable[str]) -> list[tuple[int, str]]:
    """(rank, lower-case word) per distinct recognisable subject, input order.
    Brands keep their casing."""
    base, qualifiers = normalise_labels(labels)
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for label in base:
        word = label.replace("_", " ").lower()
        if word not in seen:
            seen.add(word)
            out.append((_LABEL_RANK.get(label, _RANK_OTHER), word))
    # A bare brand in `labels` ("amazon") comes back as a qualifier.
    for qualifier in qualifiers:
        brand = BRANDS.get(qualifier.lower())
        if brand is not None and brand not in seen:
            seen.add(brand)
            out.append((_RANK_PACKAGE, brand))
    return out


def _identity_words(identities: Iterable[str]) -> list[str]:
    """Names first (as stored, first letter capitalised), then brands."""
    names: list[str] = []
    brands: list[str] = []
    seen: set[str] = set()
    for raw in identities:
        text = (raw or "").strip()
        key = text.lower()
        if not text or key in _NOT_IDENTITIES or key in seen:
            continue
        seen.add(key)
        brand = BRANDS.get(key)
        if brand is not None:
            brands.append(brand)
        else:
            names.append(_upper_first(text))
    return names + brands


def who(labels: Iterable[str], identities: Iterable[str]) -> str:
    """"Alice", "Person and dog", "Amazon", ... or "Activity" when nothing is
    recognisable."""
    words = _identity_words(identities)
    if not words:
        subjects = sorted(_subject_words(labels), key=lambda s: s[0])  # stable
        words = [w for _, w in subjects]
    if not words:
        return "Activity"
    return _upper_first(_join(words))


def route_places(
    stops: Iterable[MemberStop], zone_display: Callable[[str], str | None]
) -> list[Place]:
    """Members in start-time order -> places, consecutive duplicates collapsed.

    `zone_display` maps a raw zone key to its configured display name (or
    None); an unconfigured zone is humanised the way push copy does
    ("front_door" -> "Front Door").
    """
    places: list[Place] = []
    for stop in sorted(stops, key=lambda s: s.start):
        zone = stop.first_zone or (stop.zones[0] if stop.zones else "")
        if zone:
            place = Place(zone_display(zone) or zone.replace("_", " ").title(), True)
        elif stop.camera:
            place = Place(pretty_camera(stop.camera), False)
        else:
            continue
        if places and places[-1].name == place.name:
            continue
        places.append(place)
    return places


def encounter_title(
    labels: Iterable[str], identities: Iterable[str], places: Sequence[Place]
) -> str:
    """The full "who + route" title; see the module docstring."""
    subject = who(labels, identities)
    if not places:
        return subject
    if len(places) == 1:
        only = places[0]
        if only.is_zone:
            return f"{subject} near {only.name}"
        return f"{subject}{_DOT}{only.name} camera"
    names = [p.name for p in places]
    if len(names) > _MAX_LISTED_PLACES:
        names = [*names[:2], _ELLIPSIS, names[-1]]
    return f"{subject}{_DOT}{_ARROW.join(names)}"
