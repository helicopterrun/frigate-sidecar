"""The starter situation library and the sound catalog.

Two small, static answers to two onboarding questions the app can't answer on
its own:

* `GET /v1/push/situations/library` -- "what should I even set up?" (plan §1:
  the library *is* the onboarding answer). A new user enables one with a tap
  and adjusts its zones.
* `GET /v1/push/sounds` -- which per-situation sounds the app is known to
  bundle (plan §3: a distinct sound per situation category is most of what
  makes a push feel specific rather than generic).

Both are content the *app* ultimately owns; the sidecar publishes the set so
the situation editor has a vocabulary to render. Keyed on `app_version`
because the bundled `.caf` assets ship with the app, not the server -- a
sidecar that advertised a sound an older build doesn't contain would produce a
silent notification, which reads as broken.
"""

from __future__ import annotations

from typing import Any

from frigate_sidecar.push.situations import Situation

# Zones and cameras below are *placeholders*, using the plan's own example
# names (§1). The app's situation editor replaces them with real ones read
# from the user's Frigate `/api/config` before the situation is registered --
# a starter enabled without that step matches only if the user happens to
# have a zone by the same name, which fails silent rather than firehose.
STARTER_SITUATIONS: tuple[Situation, ...] = (
    Situation(
        id="at-the-door",
        name="At the door",
        tier="interrupt",
        cameras=("doorbell",),
        labels=("person",),
        zones=("porch",),
        loiter_seconds=5.0,
        audio_events=("doorbell",),
        sound="chime",
    ),
    Situation(
        id="near-my-car",
        name="Near my car",
        tier="interrupt",
        cameras=("driveway",),
        labels=("person",),
        zones=("driveway", "charger"),
        loiter_seconds=8.0,
        sound="knock",
    ),
    Situation(
        id="package-delivery",
        name="Package delivery",
        # Present, not Interrupt: a package arriving is worth *seeing*, not
        # worth being interrupted for (plan §2's tier table). Its delivery
        # surface is the Live Activity, which lands in Phase 2 -- until then
        # this starter is accepted and silent.
        tier="present",
        cameras=("doorbell",),
        labels=("person",),
        zones=("porch", "package_drop"),
        loiter_seconds=3.0,
        sound="marimba",
    ),
    Situation(
        id="unknown-vehicle-parked",
        name="Unknown vehicle parked",
        tier="present",
        cameras=("driveway",),
        labels=("car",),
        zones=("driveway",),
        loiter_seconds=60.0,
        # Both of these are Phase-1 no-ops (Frigate's review topic carries
        # neither a stationary flag nor, usefully, a sub-label): persisted
        # here so the rule reads correctly and the later phase that can honour
        # them doesn't have to re-author the starter.
        require_stationary=True,
        sub_label_deny=(),
        sound="pulse",
    ),
)


def starter_library() -> list[dict[str, Any]]:
    return [s.to_dict() for s in STARTER_SITUATIONS]


#: Sound ids the app bundles, newest catalog first. `id` goes in a situation's
#: `sound` field; the payload carries `<id>.caf`, which iOS resolves against
#: the *delivering* process's bundle -- so the assets must sit in both the app
#: and the NSE target (plan §9).
_CATALOG_1: tuple[tuple[str, str], ...] = (
    ("default", "Default"),
    ("chime", "Chime"),
    ("knock", "Knock"),
    ("bell", "Bell"),
    ("marimba", "Marimba"),
    ("pulse", "Pulse"),
)

#: app_version -> catalog. Phase 1 has exactly one catalog; the mapping exists
#: so adding a sound in a later app build is a one-line change here instead of
#: a protocol change.
_CATALOGS: dict[str, tuple[tuple[str, str], ...]] = {}
_DEFAULT_CATALOG = _CATALOG_1

SOUND_IDS = frozenset(sid for sid, _ in _CATALOG_1)


def sound_catalog(app_version: str = "") -> list[dict[str, str]]:
    catalog = _CATALOGS.get(app_version.strip(), _DEFAULT_CATALOG)
    return [{"id": sid, "name": name} for sid, name in catalog]


def sound_file(sound_id: str) -> str:
    """The `aps.sound` value for a situation's chosen sound.

    An unknown id falls back to the system default rather than to a filename
    iOS can't resolve: a missing `.caf` delivers the notification *silently*,
    which is indistinguishable from a bug at the moment the user most wants to
    be told something.
    """
    sid = (sound_id or "").strip()
    if not sid or sid == "default" or sid not in SOUND_IDS:
        return "default"
    return f"{sid}.caf"
