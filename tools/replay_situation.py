#!/usr/bin/env python3
"""Replay a canned Frigate review-item lifecycle onto MQTT.

Publishes a timed sequence of `frigate/reviews` messages shaped exactly like
the ones Frigate itself sends (see
`frigate_sidecar.push.decision.parse_review_message`), so the real running
pipeline -- `push.mqtt` subscriber -> `push.situations`/`push.decision` ->
`push.engine` -> `push.payload` -> transport -> APNs -> device -- can be
exercised on demand instead of waiting for a real camera event.

This tool is deliberately outside `frigate_sidecar`: it only ever emits raw
JSON that mirrors Frigate's own wire format, never imports the pipeline's own
parsing/matching code, so a bug in a scenario file can't be masked by (or
accidentally validated against) the very code it's meant to exercise.

Usage:
    python tools/replay_situation.py --scenario person-porch --dry-run
    python tools/replay_situation.py --scenario person-porch --camera doorbell
    python tools/replay_situation.py --scenario brief-detection --speed 4
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCENARIOS_DIR = Path(__file__).resolve().parent / "replay-scenarios"

# Synthetic ids always carry this prefix so they can never collide with a
# real Frigate review/event/track id, whichever transport (LogTransport's
# dry-run diff or a real RelayTransport delivering to a device) ends up
# handling the resulting push.
REPLAY_ID_PREFIX = "replay-"

DEFAULT_TOPIC = "frigate/reviews"


def resolve_scenario_path(name: str) -> Path:
    """Accept a bare scenario name (looked up in `replay-scenarios/`) or a
    path to a scenario JSON file directly."""
    direct = Path(name)
    if direct.suffix == ".json" and direct.exists():
        return direct
    candidate = SCENARIOS_DIR / f"{name}.json"
    if not candidate.exists():
        raise FileNotFoundError(
            f"no scenario named {name!r} ({candidate} does not exist)"
        )
    return candidate


def load_scenario(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"{path}: scenario must define a non-empty 'steps' list")
    for i, step in enumerate(steps):
        if step.get("type") not in ("new", "update", "end"):
            raise ValueError(f"{path}: steps[{i}].type must be new|update|end")
    return data


def build_messages(
    scenario: dict[str, Any],
    *,
    camera: str | None = None,
    label: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expand a scenario into the ordered sequence of MQTT messages a real
    Frigate would publish for one review item's lifecycle.

    Each returned item is `{"delay_s": float, "topic": str, "payload": dict}`.
    `delay_s` is the wait *before* publishing that message, unscaled by
    `--speed` -- callers apply speed at run time. `payload["after"]["start_time"]`
    and a terminal `end_time` are left as placeholders (`None`) here; the
    runner stamps them with real wall-clock values immediately before
    publishing, since that is what makes a replayed item look "live" to the
    dwell/loiter logic that reads `start_time`.
    """
    camera = camera or scenario.get("camera") or "porch"
    label = label or scenario.get("label") or "person"
    run_id = run_id or uuid.uuid4().hex[:8]
    scenario_id = scenario.get("id") or "situation"
    review_id = f"{REPLAY_ID_PREFIX}{scenario_id}-{run_id}"
    track_id = f"{review_id}-t1"

    messages: list[dict[str, Any]] = []
    prev_after: dict[str, Any] | None = None
    for step in scenario["steps"]:
        msg_type = step["type"]
        after = {
            "id": review_id,
            "camera": camera,
            "severity": step.get("severity", "detection"),
            "start_time": None,  # stamped at publish time
            "end_time": None,  # stamped at publish time, only for "end"
            "data": {
                "objects": step.get("objects", [label]),
                "detections": [track_id],
                "zones": step.get("zones", []),
                "audio": step.get("audio", []),
                "sub_labels": step.get("sub_labels", []),
            },
        }
        payload = {"type": msg_type, "before": prev_after or after, "after": after}
        messages.append(
            {
                "delay_s": float(step.get("delay_s", 0.0)),
                "topic": step.get("topic", DEFAULT_TOPIC),
                "payload": payload,
            }
        )
        prev_after = after
    return messages


def run_scenario(
    messages: list[dict[str, Any]],
    *,
    speed: float = 1.0,
    publish: Callable[[str, str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    """Walk `messages` in order, sleeping `delay_s / speed` before each
    publish and stamping `start_time`/`end_time` with the clock right before
    the message goes out."""
    if speed <= 0:
        raise ValueError("speed must be > 0")
    start_time = clock()
    for msg in messages:
        delay = msg["delay_s"] / speed
        if delay > 0:
            sleep(delay)
        payload = msg["payload"]
        after = payload["after"]
        after["start_time"] = start_time
        if payload["type"] == "end":
            after["end_time"] = clock()
        publish(msg["topic"], json.dumps(payload))


def _print_publish(topic: str, payload_json: str) -> None:
    payload = json.loads(payload_json)
    print(f">>> {topic}\n{json.dumps(payload, indent=2)}\n")


class _MqttPublisher:
    """Thin paho-mqtt wrapper: connect once, `publish(topic, payload_json)`
    per message, disconnect via `close()`. Kept separate from `run_scenario`
    so tests never need a real broker -- they pass a plain callable instead."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        client_id: str,
        username: str | None,
        password: str | None,
    ) -> None:
        import paho.mqtt.client as mqtt_client

        self._client = mqtt_client.Client(
            client_id=client_id,
            callback_api_version=mqtt_client.CallbackAPIVersion.VERSION2,
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.connect(host, port)
        self._client.loop_start()

    def __call__(self, topic: str, payload_json: str) -> None:
        self._client.publish(topic, payload_json)

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scenario", required=True,
        help="Scenario name (looked up in tools/replay-scenarios/<name>.json) "
        "or a path to a scenario JSON file.",
    )
    parser.add_argument("--camera", help="Overrides the scenario's default camera.")
    parser.add_argument("--label", help="Overrides the scenario's default object label.")
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Time-compression multiplier for inter-message delays (default 1.0 = real time).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the message sequence instead of publishing to a broker.",
    )
    parser.add_argument("--mqtt-host", help="Overrides push.mqtt_host from config.")
    parser.add_argument("--mqtt-port", type=int, help="Overrides push.mqtt_port from config.")
    parser.add_argument(
        "--config", help="Path to sidecar.yml (defaults to the normal discovery rules)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    try:
        scenario = load_scenario(resolve_scenario_path(args.scenario))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    messages = build_messages(scenario, camera=args.camera, label=args.label)

    if args.dry_run:
        run_scenario(messages, speed=args.speed, publish=_print_publish, sleep=lambda _: None)
        return 0

    from frigate_sidecar.config import load_settings

    push_settings = load_settings(args.config).push
    publisher = _MqttPublisher(
        host=args.mqtt_host or push_settings.mqtt_host,
        port=args.mqtt_port or push_settings.mqtt_port,
        client_id=f"frigate-sidecar-replay-{uuid.uuid4().hex[:8]}",
        username=push_settings.mqtt_username,
        password=push_settings.mqtt_password,
    )
    try:
        run_scenario(messages, speed=args.speed, publish=publisher)
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
