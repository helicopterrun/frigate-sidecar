"""Pure-Python linker tests (docs/encounters.md "Tests" section)."""

from __future__ import annotations

from frigate_sidecar.encounters.adjacency import Adjacency
from frigate_sidecar.encounters.linker import (
    Atom,
    LinkerConfig,
    OpenEncounter,
    decide,
    fold,
)

CFG = LinkerConfig(
    gap_s={"animal": 180.0, "person": 90.0, "vehicle": 45.0, "default": 60.0},
    max_duration_s=1800.0,
    recent_cameras=2,
    min_copresence_s=3.0,
)

ADJ = Adjacency(
    edges=frozenset(
        {
            frozenset({"alley-wide", "shed"}),
            frozenset({"shed", "stairway-wide"}),
        }
    )
)


def _atom(
    atom_id: str,
    camera: str,
    start: float,
    end: float | None = None,
    labels: tuple[str, ...] = ("raccoon",),
    zones: tuple[str, ...] = (),
    sub_labels: tuple[str, ...] = (),
    severity: str = "detection",
) -> Atom:
    return Atom(
        atom_id=atom_id,
        camera=camera,
        start_time=start,
        end_time=end,
        labels=labels,
        zones=zones,
        event_ids=(),
        sub_labels=sub_labels,
        severity=severity,
    )


def test_raccoon_chain_joins_one_encounter() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 30.0),
        _atom("a2", "shed", 150.0, 180.0),
        _atom("a3", "stairway-wide", 330.0, 360.0),
    ]
    encs = fold(atoms, ADJ, CFG)
    assert len(encs) == 1
    assert encs[0].atom_ids == ["a1", "a2", "a3"]
    assert encs[0].cameras == ["alley-wide", "shed", "stairway-wide"]


def test_same_raccoon_after_long_gap_on_non_adjacent_camera_starts_new() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 30.0),
        _atom("a2", "street", 630.0, 660.0),  # 10 min later, not adjacent
    ]
    encs = fold(atoms, ADJ, CFG)
    assert len(encs) == 2
    assert {tuple(e.atom_ids) for e in encs} == {("a1",), ("a2",)}


def test_person_non_adjacent_no_shared_zone_does_not_join() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0, labels=("person",)),
        _atom("a2", "street", 30.0, 40.0, labels=("person",)),
    ]
    encs = fold(atoms, ADJ, CFG)
    assert len(encs) == 2


def test_identity_match_joins_despite_no_adjacency() -> None:
    enc = OpenEncounter(
        encounter_id="e1",
        start_time=0.0,
        last_end=30.0,
        cameras=["alley-wide"],
        labels={"raccoon"},
        identities={"rex"},
        zones=set(),
        atom_ids=["a1"],
        peak_severity="detection",
    )
    atom = _atom("a2", "street", 500.0, 520.0, sub_labels=("rex",))
    decision = decide(atom, [enc], ADJ, CFG)
    assert decision.encounter_id == "e1"
    assert decision.reason == "identity"
    assert decision.confidence == 0.95


def test_contradictory_sub_labels_split() -> None:
    enc = OpenEncounter(
        encounter_id="e1",
        start_time=0.0,
        last_end=30.0,
        cameras=["alley-wide"],
        labels={"raccoon"},
        identities={"fido"},
        zones=set(),
        atom_ids=["a1"],
        peak_severity="detection",
    )
    atom = _atom("a2", "alley-wide", 40.0, 50.0, sub_labels=("rex",))
    decision = decide(atom, [enc], ADJ, CFG)
    assert decision.encounter_id is None
    assert decision.reason == "new"


def test_companion_beats_adjacent_when_both_eligible() -> None:
    # person+dog on camera A; dog alone shows up on the adjacent camera B
    # 5s after A's segment started, while A is still ongoing -- overlapping
    # spans make this a companionship match, which outranks the plain
    # "adjacent" continuity match (0.7 > 0.6).
    atoms = [
        _atom("a1", "alley-wide", 0.0, 20.0, labels=("person", "dog")),
        _atom("a2", "shed", 5.0, 25.0, labels=("dog",)),
    ]
    encs = fold(atoms, ADJ, CFG)
    assert len(encs) == 1
    decision = decide(
        _atom("a2", "shed", 5.0, 25.0, labels=("dog",)),
        [
            OpenEncounter(
                encounter_id="e1",
                start_time=0.0,
                last_end=20.0,
                cameras=["alley-wide"],
                labels={"person", "dog"},
                identities=set(),
                zones=set(),
                atom_ids=["a1"],
                peak_severity="detection",
            )
        ],
        ADJ,
        CFG,
    )
    assert decision.reason == "companion"
    assert decision.confidence == 0.7


def test_sealed_encounters_are_not_candidates() -> None:
    # A "sealed" encounter is simply one the caller doesn't pass in -- store.py
    # filters those out via load_open(). Simulate that here: an encounter
    # that would obviously match is simply absent from open_encounters.
    atom = _atom("a2", "alley-wide", 40.0, 50.0)
    decision = decide(atom, [], ADJ, CFG)
    assert decision.encounter_id is None
    assert decision.reason == "new"


def test_pin_and_split_decisions_honoured() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 10.0),
        _atom("a2", "street", 15.0, 25.0),  # would otherwise start a new encounter
    ]
    open_encounters = fold(atoms, ADJ, CFG)
    assert len(open_encounters) == 2
    e1 = open_encounters[0].encounter_id
    e2 = open_encounters[1].encounter_id

    # Pin forces a3 onto e1 even though nothing would naturally link it.
    pin_atom = _atom("a3", "faraway", 9999.0, 10000.0)
    decision = decide(pin_atom, open_encounters, ADJ, CFG, pinned_to=e1)
    assert decision.encounter_id == e1
    assert decision.reason == "pinned"

    # Split excludes e1 from candidacy even for an atom that would join it.
    split_atom = _atom("a4", "alley-wide", 12.0, 20.0)
    decision = decide(split_atom, open_encounters, ADJ, CFG, split_from=frozenset({e1}))
    assert decision.encounter_id != e1
    # Without e1 in play there's nothing else to join (e2 is a different
    # camera+time entirely), so this correctly starts a new encounter.
    assert decision.encounter_id is None
    assert e2 != e1  # sanity: the two folded atoms really are separate encounters


def test_fold_is_idempotent() -> None:
    atoms = [
        _atom("a1", "alley-wide", 0.0, 30.0),
        _atom("a2", "shed", 150.0, 180.0),
        _atom("a3", "street", 900.0, 920.0),
    ]
    first = fold(atoms, ADJ, CFG)
    second = fold(atoms, ADJ, CFG)
    first_groups = {frozenset(e.atom_ids) for e in first}
    second_groups = {frozenset(e.atom_ids) for e in second}
    assert first_groups == second_groups


def test_max_duration_cap_starts_new_encounter() -> None:
    enc = OpenEncounter(
        encounter_id="e1",
        start_time=0.0,
        last_end=10.0,
        cameras=["alley-wide"],
        labels={"raccoon"},
        identities=set(),
        zones=set(),
        atom_ids=["a1"],
        peak_severity="detection",
    )
    atom = _atom("a2", "alley-wide", 2000.0, 2010.0)  # 2000s > max_duration_s
    decision = decide(atom, [enc], ADJ, CFG)
    assert decision.encounter_id is None
    assert decision.reason == "new"
