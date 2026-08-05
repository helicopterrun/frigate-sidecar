"""Payload shape and thumbnail pre-warm (plan §8, §4 lever 1)."""

from __future__ import annotations

import io

from frigate_sidecar.push.library import sound_catalog, sound_file
from frigate_sidecar.push.payload import (
    APNS_MAX_PAYLOAD_BYTES,
    body_text,
    build_payload,
    payload_size,
)
from frigate_sidecar.push.situations import Match, Situation
from frigate_sidecar.push.thumbnails import resize_jpeg

AT_THE_DOOR = Situation(
    id="at-the-door", name="At the door", labels=("person",), zones=("porch",),
    loiter_seconds=5.0, sound="chime",
)


def _match(**kw) -> Match:
    base = dict(situation=AT_THE_DOOR, track_id="t1", dwell_s=6.0, label="person", zone="porch")
    base.update(kw)
    return Match(**base)  # type: ignore[arg-type]


def test_sent_at_is_epoch_seconds_with_sub_second_resolution() -> None:
    """The app's NSE subtracts this to get sidecar -> NSE and
    sidecar -> present deltas. Whole seconds would quantise a measurement
    whose interesting range is hundreds of milliseconds."""
    p = build_payload(_match(), handle="h", server_id="s", now=1785952622.7040415)
    assert p["sent_at"] == 1785952622.704
    assert isinstance(p["sent_at"], float)


def test_every_situation_path_stamps_sent_at() -> None:
    """Stamped inside build_payload so a live match, the test button, and
    anything Phase 2 adds all carry it without having to remember to."""
    import time as _time

    before = _time.time()
    p = build_payload(_match(), handle="h", server_id="s")
    after = _time.time()
    # Half a millisecond of slack at each end: the value is rounded to the
    # millisecond, so it can land just outside the window it was taken in.
    assert before - 0.0005 <= p["sent_at"] <= after + 0.0005


def test_payload_matches_section_8() -> None:
    p = build_payload(_match(), handle="h_9f3a", server_id="s_a1b2")
    assert p["aps"]["alert"] == {"title": "At the door", "body": "Person, 6s"}
    assert p["aps"]["sound"] == "chime.caf"
    assert p["aps"]["thread-id"] == "at-the-door"
    assert p["aps"]["interruption-level"] == "time-sensitive"
    assert p["aps"]["mutable-content"] == 1
    assert p["aps"]["category"] == "situation.at-the-door"
    assert p["situation_id"] == "at-the-door"
    assert p["handle"] == "h_9f3a"
    assert p["actions_available"] == ["live-view", "snooze-15m", "mute-situation"]


def test_payload_carries_a_handle_and_never_image_bytes() -> None:
    """The non-negotiable: APNs' 4KB cap makes inline images fail outright."""
    p = build_payload(_match(), handle="h_9f3a", server_id="s1")
    flat = str(p)
    assert "base64" not in flat and "image" not in flat
    assert payload_size(p) < 500
    assert payload_size(p) < APNS_MAX_PAYLOAD_BYTES


def test_absurd_situation_name_is_trimmed_rather_than_rejected_by_apple() -> None:
    huge = Situation(id="x", name="A" * 6000, labels=("person",))
    p = build_payload(_match(situation=huge), handle="h", server_id="s")
    assert payload_size(p) <= APNS_MAX_PAYLOAD_BYTES


def test_body_shows_dwell_only_when_the_rule_asked_for_one() -> None:
    assert body_text(_match()) == "Person, 6s"
    no_loiter = Situation(id="x", name="X", labels=("person",))
    assert body_text(_match(situation=no_loiter, dwell_s=6.0)) == "Person"
    # "Person, 0s" is noise.
    assert body_text(_match(dwell_s=0.4)) == "Person"


def test_body_names_the_audio_event_when_that_is_what_fired() -> None:
    assert body_text(_match(audio="doorbell")) == "Doorbell"


def test_body_carries_the_suppressed_count(  ) -> None:
    assert body_text(_match(), suppressed=30) == "Person, 6s · +30 more"


def test_unknown_sound_falls_back_to_the_system_default() -> None:
    """A missing `.caf` delivers the notification *silently*, which is
    indistinguishable from a bug at the worst possible moment."""
    assert sound_file("chime") == "chime.caf"
    assert sound_file("no-such-sound") == "default"
    assert sound_file("") == "default"
    assert sound_file("default") == "default"


def test_sound_catalog_is_stable_for_unknown_app_versions() -> None:
    assert sound_catalog("99.0") == sound_catalog("")


# -- thumbnail ---------------------------------------------------------------


def _jpeg(width: int, height: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 30, 30)).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_resize_shrinks_to_the_longest_edge_and_keeps_aspect() -> None:
    from PIL import Image

    out = resize_jpeg(_jpeg(1280, 720), max_edge=320, quality=60)
    assert out is not None
    with Image.open(io.BytesIO(out)) as im:
        assert max(im.size) == 320
        assert abs(im.width / im.height - 1280 / 720) < 0.02


def test_resize_handles_portrait_frames() -> None:
    from PIL import Image

    out = resize_jpeg(_jpeg(720, 1280), max_edge=320)
    assert out is not None
    with Image.open(io.BytesIO(out)) as im:
        assert im.height == 320


def test_resize_does_not_upscale_a_small_frame() -> None:
    from PIL import Image

    out = resize_jpeg(_jpeg(160, 90), max_edge=320)
    assert out is not None
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (160, 90)


def test_resize_lands_in_the_target_size_band() -> None:
    """~15KB is the budget the NSE's tight memory ceiling and a cold radio
    can both afford (plan §4)."""
    out = resize_jpeg(_jpeg(1920, 1080), max_edge=320, quality=60)
    assert out is not None and len(out) < 30_000


def test_garbage_bytes_cost_the_image_not_the_push() -> None:
    assert resize_jpeg(b"not a jpeg at all") is None
    assert resize_jpeg(b"") is None
