"""User-editable attention-ladder policy (Elsinore Phase 4).

Phase 2/3 shipped the routing table (`ladder_policy.TABLE`), the zone-place
heuristic (`delivery_wire.classify_place`), and the Live Activity family
toggles as hardcoded/config-file data. Phase 4 makes all three
user-editable from the app's settings screens, backed by one JSON document
(`config/push_settings.json`, not the sidecar's sqlite DB -- the app PUTs
the whole object on Save, and round-tripping a batch document through rows
and back is pure overhead this doesn't need) and one HTTP surface
(`routes/push.py`'s `/settings` endpoints).

This module owns the settings document's shape, defaults, validation,
persistence, and *application* -- `apply_settings` is the one place that
pushes a new routing table into `ladder_policy.set_table`, so "the sidecar
applies the new settings immediately" (design doc) is just "the next
`get_active()`/`ladder_policy.TABLE` read sees it", with no cache to
invalidate anywhere else.

`ladder_policy.TABLE`'s own literal default is deliberately left alone
(`ladder_policy.py`'s docstring on `set_table`) -- `DEFAULT_ROUTING_TABLE`
below is a separate, independent literal matching the product brief's
current recommended defaults, which differ from the routing engine's own
built-in baseline (`thing` no longer defaults to `quiet` at yard/doors/
private). The two are allowed to diverge: one is "what the evaluator ships
with if nothing ever touches it" (Phase 1's own tested baseline), the other
is "what a *freshly onboarded* settings file starts the user at".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frigate_sidecar.push import ladder_policy
from frigate_sidecar.push.live_activities import FAMILIES

#: Bumped only on a breaking shape change; the app pins against it the same
#: way the card payload contract does (`delivery.CONTRACT_VERSION`).
SETTINGS_VERSION = 1

SUBJECTS = ("stranger", "known", "animal", "thing")
PLACES = ("street", "yard", "doors", "private", "off_limits")
LEVELS = ladder_policy.LEVELS

#: The routing table a freshly onboarded settings file starts the user at
#: (design doc §1, "match the v3 brief's §1.4 table exactly").
DEFAULT_ROUTING_TABLE: dict[str, dict[str, str]] = {
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

#: Zone-name -> place-class guessing heuristic (design doc §5), checked in
#: this order -- most specific/alarming classes first, broadest/catch-all
#: last. Two collisions this order exists to resolve: "front_entry_person"
#: contains both a `yard` hint ("front") and a `doors` hint ("entry") -- the
#: doc's own example expects `doors` to win, so it's checked first. And
#: "sidewalk" (an explicit `street` pattern) contains "side" (a `private`
#: pattern) as a substring -- `street` is checked before `private` so the
#: doc's own literal example pattern wins over the coincidental substring.
_GUESS_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("doors", ("door", "gate", "entry", "entrance", "window")),
    ("off_limits", ("pool", "shed", "equipment", "restricted")),
    ("street", ("street", "road", "sidewalk", "curb", "highway")),
    ("private", ("back", "side", "rear", "alley", "fence")),
    ("yard", ("driveway", "porch", "walk", "path", "front", "parking")),
)

#: Name hints for "this zone/camera could be an opening" (`available_openings`
#: in the `GET` response) -- a superset of the `doors` place-class guess
#: patterns, since a garage is an opening the openings LA family cares about
#: even though it doesn't read as a "doors" *place class* on its own.
_OPENING_NAME_HINTS = ("door", "gate", "garage", "entry", "entrance")


def guess_zone_class(name: str, cameras: tuple[str, ...] = ()) -> str:
    """Best-effort place-class guess from a zone's name, falling back to its
    camera name(s) if the zone name itself doesn't hint at anything (e.g. a
    zone named after a street address whose camera is literally called
    "street"). Defaults to `"yard"` -- the safest middle ground: not silent
    like `street`, not alarming like `doors`."""
    for candidate in (name, *cameras):
        low = candidate.lower()
        for place, patterns in _GUESS_ORDER:
            if any(p in low for p in patterns):
                return place
    return "yard"


def _looks_like_opening(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in _OPENING_NAME_HINTS)


def default_settings() -> dict[str, Any]:
    return {
        "v": SETTINGS_VERSION,
        "routing_table": {subject: dict(row) for subject, row in DEFAULT_ROUTING_TABLE.items()},
        "zone_classes": {},
        "live_activities": {family: True for family in FAMILIES} | {"opening_picks": []},
    }


def validate_settings(data: Any) -> list[str]:
    """Human-readable validation errors, empty if `data` is acceptable.
    Unknown *top-level* fields are ignored (forward compat, design doc §2);
    within the three known blocks, an unknown subject/place/family key is
    rejected outright -- those vocabularies are closed, so a typo there is
    much more likely a client bug than a field this sidecar hasn't learned
    about yet."""
    if not isinstance(data, dict):
        return ["settings must be an object"]
    errors: list[str] = []

    routing_table = data.get("routing_table")
    if not isinstance(routing_table, dict):
        errors.append("routing_table must be an object")
    else:
        unknown_subjects = set(routing_table) - set(SUBJECTS)
        if unknown_subjects:
            errors.append(f"routing_table has unknown subject(s): {sorted(unknown_subjects)}")
        for subject in SUBJECTS:
            row = routing_table.get(subject)
            if not isinstance(row, dict):
                errors.append(f"routing_table.{subject} must be an object")
                continue
            unknown_places = set(row) - set(PLACES)
            if unknown_places:
                errors.append(
                    f"routing_table.{subject} has unknown place(s): {sorted(unknown_places)}"
                )
            for place in PLACES:
                level = row.get(place)
                if level not in LEVELS:
                    errors.append(
                        f"routing_table.{subject}.{place} must be one of {LEVELS}, got {level!r}"
                    )

    zone_classes = data.get("zone_classes")
    if zone_classes is not None:
        if not isinstance(zone_classes, dict):
            errors.append("zone_classes must be an object")
        else:
            for zone, place in zone_classes.items():
                if place not in PLACES:
                    errors.append(f"zone_classes.{zone} must be one of {PLACES}, got {place!r}")

    live_activities = data.get("live_activities")
    if live_activities is not None:
        if not isinstance(live_activities, dict):
            errors.append("live_activities must be an object")
        else:
            unknown_keys = set(live_activities) - set(FAMILIES) - {"opening_picks"}
            if unknown_keys:
                errors.append(f"live_activities has unknown key(s): {sorted(unknown_keys)}")
            for family in FAMILIES:
                if family in live_activities and not isinstance(live_activities[family], bool):
                    errors.append(f"live_activities.{family} must be a boolean")
            picks = live_activities.get("opening_picks")
            if picks is not None and not (
                isinstance(picks, list) and all(isinstance(p, str) for p in picks)
            ):
                errors.append("live_activities.opening_picks must be a list of strings")

    return errors


def normalize_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Fill in anything missing or invalid from a persisted-but-partial
    document with defaults, so a settings file written before a schema
    addition (a new LA family, say) survives forever without ever handing a
    reader a `KeyError`."""
    merged = default_settings()

    routing_table = data.get("routing_table")
    if isinstance(routing_table, dict):
        for subject in SUBJECTS:
            row = routing_table.get(subject)
            if isinstance(row, dict):
                for place in PLACES:
                    if row.get(place) in LEVELS:
                        merged["routing_table"][subject][place] = row[place]

    zone_classes = data.get("zone_classes")
    if isinstance(zone_classes, dict):
        merged["zone_classes"] = {
            str(zone): place for zone, place in zone_classes.items() if place in PLACES
        }

    live_activities = data.get("live_activities")
    if isinstance(live_activities, dict):
        for family in FAMILIES:
            if isinstance(live_activities.get(family), bool):
                merged["live_activities"][family] = live_activities[family]
        picks = live_activities.get("opening_picks")
        if isinstance(picks, list):
            merged["live_activities"]["opening_picks"] = [str(p) for p in picks]

    return merged


def load_settings(path: str | Path) -> dict[str, Any]:
    """The persisted settings, merged onto defaults -- or plain defaults if
    the file is missing, unreadable, or not valid JSON. A corrupt settings
    file is not a reason to fail every card evaluation; it's a reason to
    fall back exactly as if the user had never opened settings."""
    p = Path(path)
    if not p.exists():
        return default_settings()
    try:
        with p.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    return normalize_settings(data)


def save_settings(path: str | Path, settings: dict[str, Any]) -> None:
    """Write-then-rename so a reader (or a crash mid-write) never observes a
    half-written file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(settings, f, indent=2, sort_keys=True)
    tmp.replace(p)


#: The live, in-process policy -- what `get_active()` returns and what
#: `delivery_wire.py`/`live_activities.py` actually evaluate cards against.
#: `None` means nothing has called `apply_settings`/`startup` yet.
_active: dict[str, Any] | None = None


def apply_settings(settings: dict[str, Any]) -> None:
    """Make `settings` the live policy. `ladder_policy.set_table` is called
    with a fresh copy (never the caller's own dict) so a later in-place edit
    to `settings["routing_table"]` on the caller's side can't silently
    mutate what the evaluator is using. Everything else
    (`zone_classes`/`live_activities`) is read through `get_active()` at
    call time by `delivery_wire.py`, so there's nothing further to push."""
    global _active
    ladder_policy.set_table({s: dict(row) for s, row in settings["routing_table"].items()})
    _active = settings


def get_active() -> dict[str, Any]:
    """The live policy: whatever `apply_settings`/`startup` last applied, or
    a plain, non-mutating view of the defaults if neither has ever run --
    e.g. a unit test exercising `delivery_wire` directly with no running
    app, or any pre-Phase-4 test that has no idea this module exists.

    Deliberately does **not** call `apply_settings` in that fallback case:
    doing so would push `DEFAULT_ROUTING_TABLE` into `ladder_policy.TABLE`
    on the very first uninitialized call, silently swapping the routing
    engine's own built-in default (which every earlier phase's tests are
    written against) for this module's *different* one. Only an explicit
    `apply_settings`/`startup` call may touch `ladder_policy.TABLE` -- a
    real deployment always makes one from `server.py`'s lifespan before any
    card can be evaluated, so this fallback exists purely for callers that
    never opted into Phase 4 at all.
    """
    if _active is None:
        return default_settings()
    return _active


def startup(path: str | Path) -> dict[str, Any]:
    """Load from disk (or defaults) and apply. Called once from the app's
    lifespan; `GET /v1/push/settings` calls it too if nothing has loaded a
    file yet, so the very first `GET` on a fresh install both answers the
    request and creates the file (design doc §4)."""
    settings = load_settings(path)
    apply_settings(settings)
    return settings


def reset_for_tests() -> None:
    """Test-only: drop the in-memory cache. `ladder_policy.TABLE` itself is
    restored by `tests/conftest.py`'s autouse fixture, which snapshots
    whatever it actually was before the test ran (not this module's
    idea of a default) -- the one true baseline for a test that never
    touches Phase 4 at all is the routing engine's own, unchanged."""
    global _active
    _active = None


def build_available_zones(config_path: str | Path) -> list[dict[str, Any]]:
    """`available_zones` for the `GET` response: every zone across every
    camera in Frigate's config, with the cameras that see it and a guessed
    place class the user can confirm or correct. Reuses `zones.py`'s
    existing Frigate-config reader rather than re-parsing `config.yml`."""
    from frigate_sidecar.zones import load_camera_zones

    cameras_by_zone: dict[str, set[str]] = {}
    for camera, zone_list in load_camera_zones(config_path).items():
        for zone in zone_list:
            cameras_by_zone.setdefault(zone["name"], set()).add(camera)

    return [
        {
            "zone": zone,
            "cameras": sorted(cameras),
            "guessed_class": guess_zone_class(zone, tuple(sorted(cameras))),
        }
        for zone, cameras in sorted(cameras_by_zone.items())
    ]


def build_available_openings(config_path: str | Path) -> list[str]:
    """`available_openings` for the `GET` response: zone and camera names
    that look like they could be an opening (door/gate/garage/entry), for
    the openings family's per-opening picks."""
    from frigate_sidecar.zones import load_camera_zones

    names: set[str] = set()
    for camera, zone_list in load_camera_zones(config_path).items():
        if _looks_like_opening(camera):
            names.add(camera)
        for zone in zone_list:
            if _looks_like_opening(zone["name"]):
                names.add(zone["name"])
    return sorted(names)
