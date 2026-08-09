"""The card model and mutation classifier (Elsinore Phase 2: delivery pipeline).

Pure logic, no I/O -- like `ladder.py`/`ladder_policy.py`, this module is
testable without a database, a transport, or a running app. `card_store.py`
is the sqlite-backed persistence that wraps it; `delivery.py` is the
orchestration that calls `evaluate_ladder`, classifies the mutation with this
module, and builds/sends the APNs payload.

A **card** is the unit of user-facing state: one card per subject. The same
person producing five detections over two minutes is one card, mutated in
place -- not five pushes. `Card` holds exactly the fields the mutation
classifier and sound-accounting policy need; everything else (camera name,
zone, copy) lives on the caller's side and is threaded through
`delivery.py`'s payload builder instead of duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass

from frigate_sidecar.push import ladder_policy as policy

#: Mutation kinds a new evaluation against an existing (or absent) card can
#: produce. Matches the payload contract's `mutation` field 1:1 except for
#: `SUPPRESSED`, which never reaches a payload -- it tears the card down
#: instead.
CREATE = "create"
ENRICH = "enrich"  # "gains detail": same level, new facts
ESCALATE = "escalate"  # "gets louder": new level > old level
DEESCALATE = "deescalate"  # "gets quieter": new level < old level
RESOLVE = "resolve"
SUPPRESSED = "suppressed"

#: Sound is spent at most twice by ordinary policy: once at create (if the
#: level warrants it), once at the first escalate. A third, urgent-only
#: re-sound is accounted separately (`Card.resound_count`) -- see
#: `urgent_resound_due`.
SOUND_BUDGET = 2

#: Levels the level->APNs mapping (`delivery.py`) sends a push for at all.
#: `log` and `suppressed` never reach a card as a *visible* mutation; `log`
#: cards are still recorded (for the timeline/digest, out of scope this
#: phase) but never pushed.
PUSHABLE_LEVELS = ("quiet", "notify", "urgent")

#: Levels that carry sound on their *own* push (subject to the budget above).
#: `quiet` never sounds, at create or on any mutation, per the level->APNs
#: mapping table.
SOUNDED_LEVELS = ("notify", "urgent")


def _level_index(level: str) -> int:
    return policy.LEVELS.index(level)


@dataclass
class Card:
    """Server-side, mutable state for one ongoing subject.

    `card_key` is also the APNs collapse id (delivery.py's job to build it
    consistently across re-detections of the same tracked object -- see its
    module docstring for the exact scheme). Everything below is what the
    mutation classifier and sound accounting need to decide the next push;
    copy/camera/zone context is not duplicated here because it changes on
    every mutation and belongs to the caller (`delivery.py` threads it
    through the payload builder instead).
    """

    card_key: str
    level: str
    created_at: float
    updated_at: float
    #: When the *current* level became true. Resets on create/escalate/
    #: deescalate (the state changed); held steady across enrich (same level,
    #: new facts) and resolve (the elapsed time being reported is how long
    #: the resolved state held, not the instant of resolution). This is the
    #: clock the payload's `state_since_ts` reports -- "how long the state
    #: has been true", not since the first detector event ever seen.
    state_since_at: float = 0.0
    #: Sounds already spent against `SOUND_BUDGET` (create + first escalate).
    #: Does *not* include the urgent re-sound, which is tracked separately.
    sound_count: int = 0
    #: True once this card has been marked handled (config-gated, urgent
    #: only) -- stops any further urgent re-sound.
    handled: bool = False
    handled_at: float | None = None
    #: When this card last emitted a sound at all (ordinary budget or
    #: re-sound) -- the clock the urgent re-sound timer measures against.
    last_sound_at: float | None = None
    #: How many urgent re-sounds have fired. Capped at 1 by policy (`"at most
    #: once"`); kept as a count rather than a bool so a policy change to allow
    #: more doesn't need a schema change.
    resound_count: int = 0
    resolved: bool = False
    closed: bool = False  # true after SUPPRESSED or a terminal resolve is acked

    @property
    def sound_budget_remaining(self) -> bool:
        return self.sound_count < SOUND_BUDGET


def classify_mutation(existing: Card | None, new_level: str, *, resolved: bool = False) -> str:
    """Classify a new evaluation against the card store's current state for
    this card key. `new_level` is `ladder.SUPPRESSED` or one of
    `ladder_policy.LEVELS`; `resolved` is a separate, explicit signal --
    "the subject is gone" (door closed, package brought inside, camera back
    online) -- because that is not something re-running the ladder can
    infer on its own. A `thing` at a non-street place, for instance, never
    evaluates below `quiet` (`ladder_policy.TABLE`), so there is no `new_level`
    a caller could pass that would mean "resolved" -- the wire-up layer has
    to say so directly, off whatever end-of-object/end-of-condition signal
    the source (Frigate object end, door-closed sensor, `frigate/available`)
    actually provides.

    This function does not decide *whether* to push (that's the level->APNs
    table) or *whether it sounds* (that's `should_sound`) -- it only answers
    "what kind of change is this", which both of those consult.

    Mute beats resolve, matching the ladder's own "mute beats everything"
    rule: a muted, now-gone subject is still reported as `SUPPRESSED`, not
    `RESOLVE`, so it closes the same way every other muted card does.
    """
    if new_level == SUPPRESSED:
        return SUPPRESSED
    if resolved:
        return RESOLVE
    if existing is None or existing.closed:
        return CREATE
    old_idx, new_idx = _level_index(existing.level), _level_index(new_level)
    if new_idx > old_idx:
        return ESCALATE
    if new_idx < old_idx:
        return DEESCALATE
    return ENRICH


def should_sound(card: Card, mutation: str, new_level: str) -> bool:
    """Whether *this* mutation spends a sound, against the per-card budget
    (`SOUND_BUDGET = 2`: create + first escalate). `quiet` never sounds even
    when budget remains -- the level->APNs table says so regardless of
    mutation kind. Deescalate/enrich/resolve never sound by definition (the
    ladder table in the design doc).

    Budget accounting is by *sounds emitted*, not by beats: a quiet create
    emits no sound and does not spend budget, so a card's first-ever
    escalation past `quiet` can still be sound #1, and a second escalation
    (e.g. quiet -> notify -> urgent) can be sound #2.
    """
    if mutation not in (CREATE, ESCALATE):
        return False
    if new_level not in SOUNDED_LEVELS:
        return False
    return card.sound_budget_remaining


def urgent_resound_due(card: Card, *, now: float, interval_s: float, enabled: bool) -> bool:
    """True if an `urgent` card that hasn't been handled should re-sound.

    Config-gated (`enabled`, default on for urgent only per the design doc);
    fires once per card lifetime (`resound_count == 0`); measured from the
    card's last sound, not from creation, so a card that escalates late still
    gets the full interval before its first re-sound check.
    """
    if not enabled:
        return False
    if card.level != "urgent" or card.resolved or card.closed or card.handled:
        return False
    if card.resound_count > 0:
        return False
    if card.last_sound_at is None:
        return False
    return (now - card.last_sound_at) >= interval_s
