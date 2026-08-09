"""The attention ladder's golden suite (`fixtures/ladder/ladder_cases.json`).

Every case pins one precedence rule from `ladder.py`'s evaluation order --
the file header there cross-references which. A policy edit in
`ladder_policy.py` that changes any case's outcome must update the fixture
deliberately; this test never regenerates it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from frigate_sidecar.push.ladder import Snapshot, evaluate_ladder

CASES_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "ladder" / "ladder_cases.json"
CASES = json.loads(CASES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[f"{c['id']}-{c['name']}" for c in CASES])
def test_ladder_case(case):
    snapshot = Snapshot(**case["inputs"])
    assert evaluate_ladder(snapshot) == case["expected"]


def test_golden_suite_is_complete():
    """Guards against a silently truncated fixture file."""
    assert len(CASES) == 23
