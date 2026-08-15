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
SUBJECTS_V2 = ("person", "vehicle", "animal", "thing")
PLACES = ("street", "yard", "doors", "private", "off_limits")
LEVELS = ladder_policy.LEVELS
RECOGNITION_MODES = ("off", "relax_one", "relax_to_quiet")

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

DEFAULT_ROUTING_TABLE_V2: dict[str, dict[str, str]] = {
    "person": {
        "street": "log", "yard": "quiet", "doors": "notify",
        "private": "notify", "off_limits": "urgent",
    },
    "vehicle": {
        "street": "log", "yard": "quiet", "doors": "quiet",
        "private": "quiet", "off_limits": "notify",
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

DEFAULT_RECOGNITION: dict[str, str] = {
    "known_person": "relax_one",
    "known_vehicle": "relax_one",
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


def _valid_time(val: str) -> bool:
    try:
        parts = val.split(":")
        if len(parts) != 2:
            return False
        h, m = int(parts[0]), int(parts[1])
        return 0 <= h <= 23 and 0 <= m <= 59
    except (ValueError, TypeError):
        return False


def _time_to_minutes(val: str) -> int:
    h, m = val.split(":")
    return int(h) * 60 + int(m)


def is_quiet_hours(settings: dict[str, Any], now_minutes: int) -> tuple[bool, str]:
    """Check if `now_minutes` (minutes since midnight, local) falls in the
    quiet-hours window. Returns `(active, mode)`. Wrap-around ranges
    (e.g. 22:00–07:00) are supported."""
    qh = settings.get("quiet_hours")
    if not isinstance(qh, dict):
        return False, ""
    try:
        start = _time_to_minutes(qh["start"])
        end = _time_to_minutes(qh["end"])
        mode = qh.get("mode", "cap_quiet")
    except (KeyError, ValueError, TypeError):
        return False, ""
    if start <= end:
        active = start <= now_minutes < end
    else:
        active = now_minutes >= start or now_minutes < end
    return active, mode if active else ""


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
        "routing_table_v2": {subject: dict(row) for subject, row in DEFAULT_ROUTING_TABLE_V2.items()},
        "recognition": dict(DEFAULT_RECOGNITION),
        "zone_classes": {},
        "zone_overrides": {},
        "live_activities": {family: True for family in FAMILIES} | {
            "opening_picks": [],
            "delivery": "la_first",
            "alert_all_changes": False,
            "la_only": False,
        },
        "escalation_sound": "urgent",
        "mute_sounds": True,
        "quiet_hours": None,
        # camera -> [cameras that watch the same physical approach]. Dedup
        # merges same-kind stories across declared neighbors even when their
        # zone sets are disjoint (the stairway-tight/walkway split,
        # 2026-08-14). Symmetric at read time -- declaring one direction is
        # enough. Config-side only for now; the app never writes it.
        "camera_neighbors": {},
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
    if routing_table is not None:
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

    routing_table_v2 = data.get("routing_table_v2")
    if routing_table_v2 is not None:
        if not isinstance(routing_table_v2, dict):
            errors.append("routing_table_v2 must be an object")
        else:
            unknown_subjects = set(routing_table_v2) - set(SUBJECTS_V2)
            if unknown_subjects:
                errors.append(f"routing_table_v2 has unknown subject(s): {sorted(unknown_subjects)}")
            for subject in SUBJECTS_V2:
                row = routing_table_v2.get(subject)
                if not isinstance(row, dict):
                    errors.append(f"routing_table_v2.{subject} must be an object")
                    continue
                unknown_places = set(row) - set(PLACES)
                if unknown_places:
                    errors.append(
                        f"routing_table_v2.{subject} has unknown place(s): {sorted(unknown_places)}"
                    )
                for place in PLACES:
                    level = row.get(place)
                    if level not in LEVELS:
                        errors.append(
                            f"routing_table_v2.{subject}.{place} must be one of {LEVELS}, "
                            f"got {level!r}"
                        )

    recognition = data.get("recognition")
    if recognition is not None:
        if not isinstance(recognition, dict):
            errors.append("recognition must be an object")
        else:
            for key in ("known_person", "known_vehicle"):
                val = recognition.get(key)
                if val is not None and val not in RECOGNITION_MODES:
                    errors.append(
                        f"recognition.{key} must be one of {RECOGNITION_MODES}, got {val!r}"
                    )

    zone_classes = data.get("zone_classes")
    if zone_classes is not None:
        if not isinstance(zone_classes, dict):
            errors.append("zone_classes must be an object")
        else:
            for zone, place in zone_classes.items():
                if place not in PLACES:
                    errors.append(f"zone_classes.{zone} must be one of {PLACES}, got {place!r}")

    zone_overrides = data.get("zone_overrides")
    if zone_overrides is not None:
        if not isinstance(zone_overrides, dict):
            errors.append("zone_overrides must be an object")
        else:
            # Zone names themselves are unrestricted (design doc §4 -- the
            # user might configure a zone before it appears in Frigate);
            # only the inner subject/level vocabulary is closed.
            valid_subjects = set(SUBJECTS) | set(SUBJECTS_V2)
            for zone, row in zone_overrides.items():
                if not isinstance(row, dict):
                    errors.append(f"zone_overrides.{zone} must be an object")
                    continue
                for subject, level in row.items():
                    if subject not in valid_subjects:
                        errors.append(
                            f"zone_overrides.{zone} has unknown subject {subject!r}, "
                            f"must be one of {sorted(valid_subjects)}"
                        )
                        continue
                    if level not in LEVELS:
                        errors.append(
                            f"zone_overrides.{zone}.{subject} must be one of {LEVELS}, "
                            f"got {level!r}"
                        )

    live_activities = data.get("live_activities")
    if live_activities is not None:
        if not isinstance(live_activities, dict):
            errors.append("live_activities must be an object")
        else:
            for family in FAMILIES:
                if family in live_activities and not isinstance(live_activities[family], bool):
                    errors.append(f"live_activities.{family} must be a boolean")
            picks = live_activities.get("opening_picks")
            if picks is not None and not (
                isinstance(picks, list) and all(isinstance(p, str) for p in picks)
            ):
                errors.append("live_activities.opening_picks must be a list of strings")
            aac = live_activities.get("alert_all_changes")
            if aac is not None and not isinstance(aac, bool):
                errors.append("live_activities.alert_all_changes must be a boolean")
            lao = live_activities.get("la_only")
            if lao is not None and not isinstance(lao, bool):
                errors.append("live_activities.la_only must be a boolean")
            delivery = live_activities.get("delivery")
            if delivery is not None and delivery not in ("la_first", "notifications"):
                errors.append(
                    "live_activities.delivery must be 'la_first' or 'notifications'"
                )

    mute_sounds = data.get("mute_sounds")
    if mute_sounds is not None and not isinstance(mute_sounds, bool):
        errors.append("mute_sounds must be a boolean")

    quiet_hours = data.get("quiet_hours")
    if quiet_hours is not None:
        if not isinstance(quiet_hours, dict):
            errors.append("quiet_hours must be an object or null")
        else:
            for field in ("start", "end"):
                val = quiet_hours.get(field)
                if not isinstance(val, str) or not _valid_time(val):
                    errors.append(f"quiet_hours.{field} must be HH:MM (got {val!r})")
            mode = quiet_hours.get("mode")
            if mode not in ("cap_quiet", "mute_sounds"):
                errors.append(
                    f"quiet_hours.mode must be 'cap_quiet' or 'mute_sounds', got {mode!r}"
                )

    camera_neighbors = data.get("camera_neighbors")
    if camera_neighbors is not None:
        if not isinstance(camera_neighbors, dict):
            errors.append("camera_neighbors must be an object of camera -> [cameras]")
        else:
            for cam, neighbors in camera_neighbors.items():
                if not isinstance(neighbors, list) or not all(
                    isinstance(n, str) for n in neighbors
                ):
                    errors.append(
                        f"camera_neighbors[{cam!r}] must be a list of camera names"
                    )

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

    routing_table_v2 = data.get("routing_table_v2")
    if isinstance(routing_table_v2, dict):
        for subject in SUBJECTS_V2:
            row = routing_table_v2.get(subject)
            if isinstance(row, dict):
                for place in PLACES:
                    if row.get(place) in LEVELS:
                        merged["routing_table_v2"][subject][place] = row[place]

    recognition = data.get("recognition")
    if isinstance(recognition, dict):
        for key in ("known_person", "known_vehicle"):
            val = recognition.get(key)
            if val in RECOGNITION_MODES:
                merged["recognition"][key] = val

    zone_classes = data.get("zone_classes")
    if isinstance(zone_classes, dict):
        merged["zone_classes"] = {
            str(zone): place for zone, place in zone_classes.items() if place in PLACES
        }

    zone_overrides = data.get("zone_overrides")
    if isinstance(zone_overrides, dict):
        cleaned: dict[str, dict[str, str]] = {}
        for zone, row in zone_overrides.items():
            if not isinstance(row, dict):
                continue
            valid_subjects = set(SUBJECTS) | set(SUBJECTS_V2)
            valid_row = {
                subject: level
                for subject, level in row.items()
                if subject in valid_subjects and level in LEVELS
            }
            # An override that ends up empty after filtering (or was saved
            # empty to begin with) is removed, not kept as a no-op entry
            # (design doc §4's "cleaned up on save").
            if valid_row:
                cleaned[str(zone)] = valid_row
        merged["zone_overrides"] = cleaned

    live_activities = data.get("live_activities")
    if isinstance(live_activities, dict):
        for family in FAMILIES:
            if isinstance(live_activities.get(family), bool):
                merged["live_activities"][family] = live_activities[family]
        picks = live_activities.get("opening_picks")
        if isinstance(picks, list):
            merged["live_activities"]["opening_picks"] = [str(p) for p in picks]
        if isinstance(live_activities.get("alert_all_changes"), bool):
            merged["live_activities"]["alert_all_changes"] = live_activities["alert_all_changes"]
        if isinstance(live_activities.get("la_only"), bool):
            merged["live_activities"]["la_only"] = live_activities["la_only"]
        delivery = live_activities.get("delivery")
        if delivery in ("la_first", "notifications"):
            merged["live_activities"]["delivery"] = delivery

    escalation_sound = data.get("escalation_sound")
    if isinstance(escalation_sound, str) and escalation_sound:
        merged["escalation_sound"] = escalation_sound

    if isinstance(data.get("mute_sounds"), bool):
        merged["mute_sounds"] = data["mute_sounds"]

    quiet_hours = data.get("quiet_hours")
    if quiet_hours is None:
        merged["quiet_hours"] = None
    elif isinstance(quiet_hours, dict):
        start = quiet_hours.get("start", "")
        end = quiet_hours.get("end", "")
        mode = quiet_hours.get("mode", "cap_quiet")
        if _valid_time(start) and _valid_time(end) and mode in ("cap_quiet", "mute_sounds"):
            merged["quiet_hours"] = {"start": start, "end": end, "mode": mode}

    camera_neighbors = data.get("camera_neighbors")
    if isinstance(camera_neighbors, dict):
        cleaned_neighbors: dict[str, list[str]] = {}
        for cam, neighbors in camera_neighbors.items():
            if not isinstance(neighbors, list):
                continue
            names = [str(n) for n in neighbors if isinstance(n, str) and n and n != cam]
            if names:
                cleaned_neighbors[str(cam)] = names
        merged["camera_neighbors"] = cleaned_neighbors

    return merged


def camera_neighbor_set(camera: str, settings: dict[str, Any] | None = None) -> frozenset[str]:
    """Cameras declared adjacent to `camera`, symmetric closure -- declaring
    `a: [b]` makes `b` a neighbor of `a` AND `a` a neighbor of `b`."""
    table = (settings if settings is not None else get_active()).get("camera_neighbors", {})
    if not isinstance(table, dict):
        return frozenset()
    out = {str(n) for n in table.get(camera, []) if n}
    out |= {str(cam) for cam, neighbors in table.items()
            if isinstance(neighbors, list) and camera in neighbors}
    out.discard(camera)
    return frozenset(out)


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
    """Make `settings` the live policy. `ladder_policy.set_table`/
    `set_zone_overrides` are called with fresh copies (never the caller's
    own dicts) so a later in-place edit on the caller's side can't silently
    mutate what the evaluator is using. Everything else
    (`zone_classes`/`live_activities`) is read through `get_active()` at
    call time by `delivery_wire.py`, so there's nothing further to push.

    When `routing_table_v2` is present, it is the routing authority — the
    ladder TABLE gets v2 subjects (person/vehicle/animal/thing). Otherwise
    the legacy `routing_table` (stranger/known/animal/thing) is used."""
    global _active
    table = settings.get("routing_table_v2") or settings["routing_table"]
    ladder_policy.set_table({s: dict(row) for s, row in table.items()})
    ladder_policy.set_zone_overrides(
        {zone: dict(row) for zone, row in settings.get("zone_overrides", {}).items()}
    )
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
    request and creates the file (design doc §4).

    Triggers the v1→v2 routing table migration if ``routing_table_v2`` is
    absent from the loaded settings (design brief: "derives it once from
    the legacy table")."""
    import logging

    p = Path(path)
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            with p.open() as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    if not isinstance(raw, dict):
        raw = {}

    needs_migration = "routing_table_v2" not in raw
    settings = normalize_settings(raw) if raw else default_settings()

    if needs_migration and raw.get("routing_table"):
        v2_table, recognition, log_msg = migrate_v1_to_v2(raw["routing_table"])
        settings["routing_table_v2"] = v2_table
        settings["recognition"] = recognition
        logging.getLogger(__name__).info(log_msg)
        save_settings(path, settings)

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


def migrate_v1_to_v2(
    legacy: dict[str, dict[str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, str], str]:
    """Derive a v2 routing table and recognition settings from a legacy v1
    table. Returns ``(routing_table_v2, recognition, log_message)``.

    Migration rules (design brief):
    - ``person`` := legacy ``stranger`` row
    - ``vehicle`` := legacy ``thing`` row, bumped one tier at doors/off_limits
    - ``animal`` := legacy ``animal`` row (copy)
    - ``thing`` := legacy ``thing`` row (copy)
    - ``recognition.known_person``: ``relax_one`` if the legacy ``known`` row
      averaged one tier below ``stranger``, else ``off``
    - ``recognition.known_vehicle``: ``relax_one`` (no legacy equivalent)
    """
    level_idx = {lvl: i for i, lvl in enumerate(LEVELS)}

    stranger_row = legacy.get("stranger", DEFAULT_ROUTING_TABLE["stranger"])
    known_row = legacy.get("known", DEFAULT_ROUTING_TABLE["known"])
    animal_row = legacy.get("animal", DEFAULT_ROUTING_TABLE["animal"])
    thing_row = legacy.get("thing", DEFAULT_ROUTING_TABLE["thing"])

    person_row = dict(stranger_row)

    vehicle_row = dict(thing_row)
    for place in ("doors", "off_limits"):
        idx = level_idx.get(vehicle_row.get(place, "log"), 0)
        bumped = min(idx + 1, len(LEVELS) - 1)
        vehicle_row[place] = LEVELS[bumped]

    v2_table = {
        "person": person_row,
        "vehicle": vehicle_row,
        "animal": dict(animal_row),
        "thing": dict(thing_row),
    }

    stranger_avg = sum(level_idx.get(stranger_row.get(p, "log"), 0) for p in PLACES) / len(PLACES)
    known_avg = sum(level_idx.get(known_row.get(p, "log"), 0) for p in PLACES) / len(PLACES)
    gap = stranger_avg - known_avg
    known_person_mode = "relax_one" if gap >= 0.8 else "off"

    recognition = {"known_person": known_person_mode, "known_vehicle": "relax_one"}

    log_msg = (
        f"routing v2 migration: person := stranger row; "
        f"vehicle := thing row bumped one tier at doors/off_limits; "
        f"stranger_avg={stranger_avg:.2f} known_avg={known_avg:.2f} gap={gap:.2f} "
        f"-> known_person={known_person_mode}"
    )
    return v2_table, recognition, log_msg


def probe_recognition_available(config_path: str | Path) -> dict[str, bool]:
    """Check Frigate's config for face recognition and LPR capability."""
    import yaml

    result = {"faces": False, "plates": False}
    p = Path(config_path)
    if not p.exists():
        return result
    try:
        with p.open() as f:
            cfg = yaml.safe_load(f)
    except Exception:
        return result
    if not isinstance(cfg, dict):
        return result
    fr = cfg.get("face_recognition")
    if isinstance(fr, dict) and fr.get("enabled"):
        result["faces"] = True
    lpr = cfg.get("lpr")
    if isinstance(lpr, dict) and lpr.get("enabled"):
        result["plates"] = True
    return result


#: Zone key -> Frigate `friendly_name`, loaded at startup from config.yml.
#: Module-level like the routing table: the copy builder has no path to the
#: Frigate section of settings, and display names change only with Frigate's
#: own config (a sidecar restart follows those anyway).
_zone_display_names: dict[str, str] = {}


def load_zone_display_names(config_path: str | Path) -> None:
    """Read Frigate zone `friendly_name`s for push copy. Missing file or
    names → empty map; the copy builder falls back to humanizing the key.
    Zone names like `front_entry_person` are *rule* names — 'Person at Front
    Entry Person' read like a stutter on a real lock screen (2026-08-14)."""
    from frigate_sidecar.zones import load_camera_zones

    names: dict[str, str] = {}
    for _camera, zone_list in load_camera_zones(config_path).items():
        for zone in zone_list:
            friendly = zone.get("friendly_name")
            if friendly:
                names[zone["name"]] = str(friendly)
    global _zone_display_names
    _zone_display_names = names


def zone_display_name(zone: str) -> str | None:
    return _zone_display_names.get(zone)


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
