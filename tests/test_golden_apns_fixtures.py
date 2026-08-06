"""Golden APNs payload fixtures for the iOS app to replay with `simctl push`.

Each fixture under `fixtures/apns/` is the exact JSON body one of the push
builders in `frigate_sidecar.push.payload` / `frigate_sidecar.push.activity`
produces for a fixed, canned input -- deterministic `sent_at`/`timestamp`,
fixed handles and ids, so the file on disk never changes unless a builder's
*output* does.

That makes a payload-shape change a loud, reviewable diff here instead of a
phone that silently stops buzzing: this is the contract test for "what the
sidecar actually sends", and the fixtures double as replay input for
`xcrun simctl push` (see `fixtures/apns/README.md`).

Set `APNS_GOLDEN_REGEN=1` to rewrite the fixtures to match the current
builder output (after reviewing that the change is intentional).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from frigate_sidecar.push import activity
from frigate_sidecar.push.library import sound_file
from frigate_sidecar.push.payload import build_payload
from frigate_sidecar.push.situations import Escalation, Match, Situation

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "apns"

#: The iOS app's real bundle id (Elsinore.xcodeproj `PRODUCT_BUNDLE_IDENTIFIER`).
#: APNs' own "Simulator Target Bundle" key lets `simctl push` route a payload
#: without a bundle-id argument on the command line.
SIMULATOR_TARGET_BUNDLE = "com.houseofpaimon.Elsinore"

#: Fixed inputs. Never `time.time()` -- a golden fixture has to be the same
#: file every run, not just the same shape.
SENT_AT = 1_785_952_622.704
HANDLE = "h_9f3a1c2d3e4f5061"
SERVER_ID = "s_a1b2c3"
CAMERA = "doorbell"

AT_THE_DOOR = Situation(
    id="at-the-door", name="At the door", tier="interrupt",
    cameras=(CAMERA,), labels=("person",), zones=("porch",),
    loiter_seconds=5.0, sound="at-the-door",
)

PACKAGE_DELIVERY = Situation(
    id="package-delivery", name="Package delivery", tier="present",
    cameras=(CAMERA,), labels=("person",), zones=("porch",),
    loiter_seconds=3.0, sound="package-delivery",
    escalation=Escalation(from_tier="present", to_tier="interrupt",
                          kind="loiter_exceeds", threshold=5.0),
)


def _with_bundle(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "Simulator Target Bundle": SIMULATOR_TARGET_BUNDLE}


def _alert_interrupt_tier() -> dict[str, Any]:
    """A situation authored at Interrupt: a plain time-sensitive banner."""
    match = Match(situation=AT_THE_DOOR, track_id="t1", dwell_s=6.0,
                  label="person", zone="porch")
    return _with_bundle(build_payload(
        match, handle=HANDLE, server_id=SERVER_ID, now=SENT_AT,
    ))


def _alert_present_tier() -> dict[str, Any]:
    """The Phase-2 fallback rule: a Present situation on a device with no
    push-to-start token falls back to the same alert shape (handoff item 9,
    "the app works without Phase 2")."""
    match = Match(situation=PACKAGE_DELIVERY, track_id="t2", dwell_s=4.0,
                  label="person", zone="porch")
    return _with_bundle(build_payload(
        match, handle=HANDLE, server_id=SERVER_ID, now=SENT_AT,
    ))


def _live_activity_start() -> dict[str, Any]:
    match = Match(situation=PACKAGE_DELIVERY, track_id="t3", dwell_s=0.0,
                  label="person", zone="porch")
    return _with_bundle(activity.build_start(
        match, handle=HANDLE, camera=CAMERA, server_id=SERVER_ID,
        thumbnail_revision=1, now=SENT_AT,
    ))


def _live_activity_update() -> dict[str, Any]:
    match = Match(situation=PACKAGE_DELIVERY, track_id="t3", dwell_s=2.0,
                  label="person", zone="porch")
    return _with_bundle(activity.build_update(
        match, stage="present", thumbnail_revision=1, now=SENT_AT,
    ))


def _live_activity_escalation() -> dict[str, Any]:
    """The `event: update` push that also buzzes -- one push carrying both
    an advancing ContentState and an `alert` + `sound` at the `aps` level."""
    match = Match(situation=PACKAGE_DELIVERY, track_id="t3", dwell_s=6.0,
                  label="person", zone="porch")
    return _with_bundle(activity.build_escalation(
        match, sound=sound_file(PACKAGE_DELIVERY.sound),
        thumbnail_revision=2, now=SENT_AT,
    ))


def _live_activity_end() -> dict[str, Any]:
    match = Match(situation=PACKAGE_DELIVERY, track_id="t3", dwell_s=45.0,
                  label="person", zone="porch")
    return _with_bundle(activity.build_end(
        match, thumbnail_revision=2, now=SENT_AT,
    ))


#: (fixture filename, builder). One representative payload per shape.
FIXTURE_SPECS: tuple[tuple[str, Any], ...] = (
    ("alert-interrupt-tier.apns", _alert_interrupt_tier),
    ("alert-present-tier.apns", _alert_present_tier),
    ("live-activity-start.apns", _live_activity_start),
    ("live-activity-update.apns", _live_activity_update),
    ("live-activity-escalation.apns", _live_activity_escalation),
    ("live-activity-end.apns", _live_activity_end),
)


def _regen_requested() -> bool:
    return os.environ.get("APNS_GOLDEN_REGEN", "") not in ("", "0", "false", "False")


@pytest.mark.parametrize("filename,builder", FIXTURE_SPECS)
def test_golden_apns_fixture(filename: str, builder: Any) -> None:
    path = FIXTURES_DIR / filename
    built = builder()
    rendered = json.dumps(built, indent=2, sort_keys=False) + "\n"

    if _regen_requested() or not path.exists():
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        return

    on_disk = path.read_text()
    if on_disk != rendered:
        pytest.fail(
            f"{path} no longer matches what the push builders produce.\n\n"
            "If this change is intentional, review the diff below and "
            "re-bless the fixture with:\n\n"
            f"    APNS_GOLDEN_REGEN=1 pytest {Path(__file__).name}\n\n"
            f"--- fixture on disk ({path}) ---\n{on_disk}\n"
            f"--- current builder output ---\n{rendered}"
        )


def test_every_fixture_file_is_covered_by_a_builder() -> None:
    """Nothing stale sits in the directory unaccounted for."""
    on_disk = {p.name for p in FIXTURES_DIR.glob("*.apns")}
    covered = {name for name, _ in FIXTURE_SPECS}
    assert on_disk == covered
