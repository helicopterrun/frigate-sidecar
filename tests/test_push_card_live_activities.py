"""Unit tests for `push/live_activities.py`: family detection, glyph
mapping, and the three card-model Live Activity payload shapes (Elsinore
Phase 3)."""

from __future__ import annotations

from frigate_sidecar.push import live_activities as la
from frigate_sidecar.push.cards import CREATE, ENRICH, RESOLVE


def test_package_family():
    assert la.should_start_activity(
        subject_kind="thing", label="package", place_class="yard",
    ) == la.PACKAGE


def test_bins_family_matches_bin_and_truck_labels():
    assert la.should_start_activity(
        subject_kind="thing", label="waste_bin", place_class="street",
    ) == la.BINS
    assert la.should_start_activity(
        subject_kind="thing", label="garbage_truck", place_class="street",
    ) == la.BINS


def test_openings_family_matches_door_gate_garage():
    for label in ("door", "gate", "garage"):
        assert la.should_start_activity(
            subject_kind="thing", label=label, place_class="doors",
        ) == la.OPENINGS


def test_person_family_requires_doors_place_class():
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="doors",
    ) == la.PERSON
    assert la.should_start_activity(
        subject_kind="known", label="person", place_class="doors",
    ) == la.PERSON
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="yard",
    ) is None


def test_non_qualifying_cards_return_none():
    assert la.should_start_activity(
        subject_kind="animal", label="dog", place_class="yard",
    ) is None
    assert la.should_start_activity(
        subject_kind="thing", label="car", place_class="street",
    ) is None
    assert la.should_start_activity(
        subject_kind="stranger", label="person", place_class="street",
    ) is None


def test_opening_picks_empty_is_permissive():
    # Nothing curated yet -- every opening qualifies (Elsinore Phase 4).
    assert la.should_start_activity(
        subject_kind="thing", label="garage", place_class="street",
        opening_picks=[], opening_ids=("garage-cam",),
    ) == la.OPENINGS


def test_opening_picks_restricts_to_curated_openings():
    assert la.should_start_activity(
        subject_kind="thing", label="garage", place_class="street",
        opening_picks=["front_gate"], opening_ids=("garage-cam", "garage"),
    ) is None
    assert la.should_start_activity(
        subject_kind="thing", label="gate", place_class="street",
        opening_picks=["front_gate"], opening_ids=("front_gate", "doorbell"),
    ) == la.OPENINGS


def test_opening_picks_do_not_affect_other_families():
    assert la.should_start_activity(
        subject_kind="thing", label="package", place_class="yard",
        opening_picks=["front_gate"], opening_ids=("some-other-camera",),
    ) == la.PACKAGE


def test_disabled_family_returns_none():
    assert la.should_start_activity(
        subject_kind="thing", label="package", place_class="yard",
        families_enabled={"package": False},
    ) is None
    # Every other family stays on by default even with one explicit override.
    assert la.should_start_activity(
        subject_kind="thing", label="waste_bin", place_class="street",
        families_enabled={"package": False},
    ) == la.BINS


def test_glyph_for_resolve_wins_over_family():
    for family in la.FAMILIES:
        assert la.glyph_for(
            family, subject_kind="thing", label="package", mutation=RESOLVE,
        ) == "checkmark.circle.fill"


def test_glyph_for_person_known_vs_stranger():
    assert la.glyph_for(
        la.PERSON, subject_kind="known", label="person", mutation=ENRICH,
    ) == "figure.wave"
    assert la.glyph_for(
        la.PERSON, subject_kind="stranger", label="person", mutation=ENRICH,
    ) == "figure.walk"


def test_glyph_for_openings_by_label():
    assert la.glyph_for(
        la.OPENINGS, subject_kind="thing", label="garage", mutation=ENRICH,
    ) == "door.garage.open.trianglebadge.exclamationmark"
    assert la.glyph_for(
        la.OPENINGS, subject_kind="thing", label="gate", mutation=ENRICH,
    ) == "pedestrian.gate.open"
    assert la.glyph_for(
        la.OPENINGS, subject_kind="thing", label="door", mutation=ENRICH,
    ) == "door.left.hand.open"


def test_glyph_for_package_and_bins():
    assert la.glyph_for(
        la.PACKAGE, subject_kind="thing", label="package", mutation=CREATE,
    ) == "shippingbox.fill"
    assert la.glyph_for(
        la.BINS, subject_kind="thing", label="waste_bin", mutation=CREATE,
    ) == "trash.fill"


def _content_state(**overrides):
    base = dict(
        level="notify", mutation="create", glyph="figure.walk",
        primary="Person at front door", secondary="Just now", elapsed_seconds=0,
        card_key="doorbell:stranger:1786235300-aywxqj",
        thumbnail_handle="h_OC_02EpiY-Q", thumbnail_revision=0,
    )
    base.update(overrides)
    return la.build_content_state(**base)


def test_build_content_state_field_names_are_snake_case_and_exact():
    state = _content_state()
    assert set(state) == {
        "level", "mutation", "glyph", "primary", "secondary", "elapsed_seconds",
        "deep_link_card_key", "thumbnail_handle", "thumbnail_revision",
    }
    assert state["deep_link_card_key"] == "doorbell:stranger:1786235300-aywxqj"


def test_build_la_start_payload_shape():
    state = _content_state()
    payload = la.build_la_start_payload(
        content_state=state, family="person", camera="doorbell",
        track_id="1786235300-aywxqj", card_key="doorbell:stranger:1786235300-aywxqj",
        now=1786235302,
    )
    aps = payload["aps"]
    assert aps["timestamp"] == 1786235302
    assert aps["event"] == "start"
    assert aps["content-state"] == state
    assert aps["attributes-type"] == "ElsinoreActivityAttributes"
    assert aps["attributes"] == {
        "card_key": "doorbell:stranger:1786235300-aywxqj",
        "family": "person",
        "camera": "doorbell",
        "track_id": "1786235300-aywxqj",
    }


def test_build_la_update_payload_shape():
    state = _content_state(mutation="enrich", level="quiet")
    payload = la.build_la_update_payload(content_state=state, now=1786235310)
    assert payload["aps"]["timestamp"] == 1786235310
    assert payload["aps"]["event"] == "update"
    assert payload["aps"]["content-state"] == state
    assert "attributes" not in payload["aps"]
    assert "attributes-type" not in payload["aps"]


def test_build_la_end_payload_dismissal_is_timestamp_plus_thirty():
    state = _content_state(mutation="resolve", thumbnail_handle=None)
    payload = la.build_la_end_payload(content_state=state, now=1786235329)
    aps = payload["aps"]
    assert aps["event"] == "end"
    assert aps["timestamp"] == 1786235329
    assert aps["dismissal-date"] == 1786235359
    assert aps["relevance-score"] == 0.75  # default level is "notify"
    assert "thumbnail_handle" not in aps["content-state"]
