"""Declarative policy for the attention ladder (`ladder.py`).

Everything that decides *how loud* a detection is lives here as data: the
subject x place base table, which reasons nudge the result up or down, which
Frigate labels are dangerous enough to reclassify as a stranger, and the fixed
level a system card (e.g. "camera offline") reports at. A policy change --
adding a dangerous-animal label, moving a zone class in the table, retuning
which reasons count as worry vs. calm -- is an edit to this file, re-validated
by `tests/test_push_ladder.py` against `fixtures/ladder_cases.json`. The
evaluator in `ladder.py` never branches on any of these values by name.
"""

from __future__ import annotations

#: Ordinal, low -> high. Index arithmetic in `ladder.py` (clamp, nudge, cap,
#: floor) all assume this order.
LEVELS = ("log", "quiet", "notify", "urgent")

#: subject x place -> base level, before nudges/floor/caps.
TABLE: dict[str, dict[str, str]] = {
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
        "street": "log", "yard": "quiet", "doors": "quiet",
        "private": "quiet", "off_limits": "quiet",
    },
}

#: Frigate labels that reclassify `subject` as `stranger` regardless of its
#: upstream classification (a bear or skunk on the property outranks whatever
#: upstream called it). Never add coyote -- Frigate cannot ID it, so a
#: `label == "coyote"` never actually occurs.
DANGEROUS_ANIMAL_LABELS = frozenset({"bear", "skunk", "raccoon"})

#: Reasons that push the result one level up. Must be `Snapshot` field names
#: -- `ladder.py` reads them by `getattr`.
WORRY_REASONS = (
    "nobody_home", "night", "dwell_exceeded", "seen_before_still_unrecognized",
)

#: Reasons that pull the result one level down. Must be `Snapshot` field names.
CALM_REASONS = ("known_role", "low_confidence", "no_recognition_capability")

#: Fixed level for `source == "system"` cards (camera offline, disk full,
#: etc.) -- these have no subject or place, so they never touch the table,
#: nudge, floor, or caps.
SYSTEM_CARD_LEVEL = "notify"
