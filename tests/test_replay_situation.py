"""Tests for tools/replay_situation.py.

`tools/` sits outside the `frigate_sidecar` package (it is a dev-only CLI,
not shipped code), so the module is loaded directly from its file path
rather than imported normally.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"

_spec = importlib.util.spec_from_file_location(
    "replay_situation", TOOLS_DIR / "replay_situation.py"
)
assert _spec is not None and _spec.loader is not None
replay_situation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_situation)


def test_resolve_scenario_path_by_name():
    path = replay_situation.resolve_scenario_path("person-porch")
    assert path == TOOLS_DIR / "replay-scenarios" / "person-porch.json"
    assert path.exists()


def test_resolve_scenario_path_unknown_raises():
    with pytest.raises(FileNotFoundError):
        replay_situation.resolve_scenario_path("does-not-exist")


def test_load_scenario_rejects_missing_steps(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"camera": "porch"}))
    with pytest.raises(ValueError, match="steps"):
        replay_situation.load_scenario(bad)


def test_load_scenario_rejects_bad_step_type(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"steps": [{"type": "bogus"}]}))
    with pytest.raises(ValueError, match="new\\|update\\|end"):
        replay_situation.load_scenario(bad)


def test_build_messages_person_porch_full_lifecycle():
    path = replay_situation.resolve_scenario_path("person-porch")
    scenario = replay_situation.load_scenario(path)
    messages = replay_situation.build_messages(scenario, run_id="abc123")

    assert [m["payload"]["type"] for m in messages] == [
        "new", "update", "update", "update", "end"
    ]
    assert [m["payload"]["after"]["severity"] for m in messages] == [
        "detection", "detection", "detection", "alert", "alert"
    ]
    assert [m["delay_s"] for m in messages] == [0, 3, 4, 5, 6]
    assert all(m["topic"] == "frigate/reviews" for m in messages)

    first_after = messages[0]["payload"]["after"]
    assert first_after["id"] == "replay-person-porch-abc123"
    assert first_after["camera"] == "porch"
    assert first_after["data"]["objects"] == ["person"]
    assert first_after["data"]["detections"] == ["replay-person-porch-abc123-t1"]

    # ids stay stable and prefixed across every message in the lifecycle.
    ids = {m["payload"]["after"]["id"] for m in messages}
    assert ids == {"replay-person-porch-abc123"}
    for m in messages:
        assert m["payload"]["after"]["id"].startswith(replay_situation.REPLAY_ID_PREFIX)
        for det in m["payload"]["after"]["data"]["detections"]:
            assert det.startswith(replay_situation.REPLAY_ID_PREFIX)


def test_build_messages_respects_camera_and_label_overrides():
    path = replay_situation.resolve_scenario_path("person-porch")
    scenario = replay_situation.load_scenario(path)
    messages = replay_situation.build_messages(
        scenario, camera="doorbell", label="dog", run_id="xyz"
    )
    assert messages[0]["payload"]["after"]["camera"] == "doorbell"
    assert messages[0]["payload"]["after"]["data"]["objects"] == ["dog"]


def test_build_messages_before_tracks_previous_after():
    path = replay_situation.resolve_scenario_path("person-porch")
    scenario = replay_situation.load_scenario(path)
    messages = replay_situation.build_messages(scenario, run_id="abc123")

    # "new"'s before == after (nothing existed prior); every later message's
    # before is the previous step's after, exactly what Frigate itself sends.
    assert messages[0]["payload"]["before"] is messages[0]["payload"]["after"]
    for prev, cur in zip(messages, messages[1:], strict=False):
        assert cur["payload"]["before"] is prev["payload"]["after"]


def test_brief_detection_scenario_does_not_escalate():
    path = replay_situation.resolve_scenario_path("brief-detection")
    scenario = replay_situation.load_scenario(path)
    messages = replay_situation.build_messages(scenario, run_id="abc123")

    severities = {m["payload"]["after"]["severity"] for m in messages}
    assert severities == {"detection"}
    assert [m["payload"]["type"] for m in messages] == ["new", "end"]


def test_run_scenario_publishes_in_order_with_scaled_delays():
    path = replay_situation.resolve_scenario_path("person-porch")
    scenario = replay_situation.load_scenario(path)
    messages = replay_situation.build_messages(scenario, run_id="abc123")

    sent: list[tuple[str, dict]] = []
    slept: list[float] = []
    clock_calls = {"n": 0}

    def fake_clock() -> float:
        clock_calls["n"] += 1
        return 1000.0 + clock_calls["n"]

    def fake_publish(topic: str, payload_json: str) -> None:
        sent.append((topic, json.loads(payload_json)))

    replay_situation.run_scenario(
        messages,
        speed=2.0,
        publish=fake_publish,
        sleep=slept.append,
        clock=fake_clock,
    )

    assert [t for t, _ in sent] == ["frigate/reviews"] * 5
    assert [p["type"] for _, p in sent] == ["new", "update", "update", "update", "end"]
    # delay_s values [0, 3, 4, 5, 6] halved by speed=2.0; the leading 0 delay
    # is never slept.
    assert slept == [1.5, 2.0, 2.5, 3.0]

    # start_time is stamped once (first clock() call) and held constant.
    start_times = {p["after"]["start_time"] for _, p in sent}
    assert len(start_times) == 1
    # end_time is only stamped on the terminal "end" message.
    assert sent[-1][1]["after"]["end_time"] is not None
    assert all(p["after"]["end_time"] is None for _, p in sent[:-1])


def test_run_scenario_rejects_non_positive_speed():
    with pytest.raises(ValueError, match="speed"):
        replay_situation.run_scenario([], speed=0, publish=lambda *_: None)


def test_dry_run_cli_prints_sequence_without_broker(capsys):
    exit_code = replay_situation.main(["--scenario", "person-porch", "--dry-run"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.count(">>> frigate/reviews") == 5
    assert '"type": "new"' in out
    assert '"type": "end"' in out


def test_cli_unknown_scenario_errors_cleanly(capsys):
    exit_code = replay_situation.main(["--scenario", "nope", "--dry-run"])
    assert exit_code == 1
    assert "nope" in capsys.readouterr().err
