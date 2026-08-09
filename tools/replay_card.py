#!/usr/bin/env python3
"""Replay canned Frigate sequences through the card pipeline.

Publishes timed `frigate/reviews` and `frigate/events` messages to MQTT so
the live sidecar's card pipeline (Phase 5) runs end to end: per-device
filtering, snooze, rate cap, quiet hours, payload construction, relay/APNs,
and the app's NSE all exercise against a registered device.

Scenarios live in `tools/replay-scenarios/card-*.json`. Each step specifies
its topic (`frigate/reviews` or `frigate/events`) so the tool drives both
the review-create/enrich path and the object-end resolve path — the full
card lifecycle.

Decision logging: in `--dry-run` mode, the tool instantiates the real card
pipeline in-process (with LogTransport) and prints what the sidecar decided
at each step: card mutation, level, sounded or not, LA action, per-device
filtering outcome. In live mode (publishing to MQTT), the sidecar's own
structured log lines (`push: card mutation=...`) are the decision trace —
use `journalctl -u frigate-sidecar -f` alongside this tool.

Usage:
    python tools/replay_card.py --scenario card-notify-resolve --dry-run
    python tools/replay_card.py --scenario card-la-package --camera doorbell
    python tools/replay_card.py --scenario card-escalate-urgent --speed 4
    python tools/replay_card.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCENARIOS_DIR = Path(__file__).resolve().parent / "replay-scenarios"
REPLAY_ID_PREFIX = "replay-"


def list_scenarios() -> list[dict[str, str]]:
    results = []
    for p in sorted(SCENARIOS_DIR.glob("card-*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        results.append({
            "name": p.stem,
            "description": data.get("description", ""),
        })
    return results


def resolve_scenario_path(name: str) -> Path:
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
        topic = step.get("topic", "frigate/reviews")
        if topic not in ("frigate/reviews", "frigate/events"):
            raise ValueError(f"{path}: steps[{i}].topic must be frigate/reviews or frigate/events")
    return data


def build_messages(
    scenario: dict[str, Any],
    *,
    camera: str | None = None,
    label: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Expand a scenario into ordered MQTT messages.

    Each returned item: `{"delay_s": float, "topic": str, "payload": dict}`.
    `start_time` is stamped at publish time (now-relative) so backfill
    staleness (300s default) never discards the event.

    `frigate/events` messages use a different wire format than
    `frigate/reviews`: the `after` dict has `id` = track_id (not review_id),
    `label` (not `data.objects`), `current_zones` (not `data.zones`).
    """
    camera = camera or scenario.get("camera") or "doorbell"
    label = label or scenario.get("label") or "person"
    run_id = run_id or uuid.uuid4().hex[:8]
    scenario_id = scenario.get("id") or "card"
    review_id = f"{REPLAY_ID_PREFIX}{scenario_id}-{run_id}"
    track_id = f"{review_id}-t1"

    messages: list[dict[str, Any]] = []
    prev_review_after: dict[str, Any] | None = None
    prev_event_after: dict[str, Any] | None = None

    for step in scenario["steps"]:
        msg_type = step["type"]
        topic = step.get("topic", "frigate/reviews")
        zones = step.get("zones", [])

        if topic == "frigate/events":
            after: dict[str, Any] = {
                "id": track_id,
                "camera": camera,
                "label": label,
                "current_zones": zones,
                "entered_zones": zones,
                "start_time": None,
                "end_time": None,
                "stationary": False,
                "sub_label": step.get("sub_labels", [None])[0] if step.get("sub_labels") else None,
            }
            payload = {
                "type": msg_type,
                "before": prev_event_after or after,
                "after": after,
            }
            prev_event_after = after
        else:
            after = {
                "id": review_id,
                "camera": camera,
                "severity": step.get("severity", "alert"),
                "start_time": None,
                "end_time": None,
                "data": {
                    "objects": step.get("objects", [label]),
                    "detections": [track_id],
                    "zones": zones,
                    "audio": step.get("audio", []),
                    "sub_labels": step.get("sub_labels", []),
                },
            }
            payload = {
                "type": msg_type,
                "before": prev_review_after or after,
                "after": after,
            }
            prev_review_after = after

        messages.append({
            "delay_s": float(step.get("delay_s", 0.0)),
            "topic": topic,
            "payload": payload,
        })
    return messages


def stamp_now(payload: dict[str, Any], *, start_time: float, clock: Callable[[], float]) -> None:
    after = payload.get("after", {})
    after["start_time"] = start_time
    if payload.get("type") == "end":
        after["end_time"] = clock()


def run_scenario(
    messages: list[dict[str, Any]],
    *,
    speed: float = 1.0,
    publish: Callable[[str, str], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> None:
    if speed <= 0:
        raise ValueError("speed must be > 0")
    start_time = clock()
    for msg in messages:
        delay = msg["delay_s"] / speed
        if delay > 0:
            sleep(delay)
        stamp_now(msg["payload"], start_time=start_time, clock=clock)
        publish(msg["topic"], json.dumps(msg["payload"]))


def _print_publish(topic: str, payload_json: str) -> None:
    payload = json.loads(payload_json)
    print(f">>> {topic}\n{json.dumps(payload, indent=2)}\n")


# ---------------------------------------------------------------------------
# Dry-run: in-process card pipeline with decision logging
# ---------------------------------------------------------------------------

async def dry_run_scenario(
    messages: list[dict[str, Any]],
    *,
    speed: float = 1.0,
    camera: str = "doorbell",
) -> list[dict[str, Any]]:
    """Run the scenario through the real card pipeline in-process with
    LogTransport, returning a decision log for each step."""
    import tempfile

    from frigate_sidecar import db
    from frigate_sidecar.config import PushSection
    from frigate_sidecar.push import store
    from frigate_sidecar.push.engine import PushEngine
    from frigate_sidecar.push.transport import LogTransport

    decisions: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "sidecar.db"
        conn = db.open_sidecar(db_path)
        store.upsert_device(
            conn, apns_token="replay-device", bundle_id="com.pondhouse.Elsinore",
            environment="sandbox", cameras=[], min_severity="detection",
            push_to_start_token="pts-replay",
        )
        conn.commit()
        conn.close()

        transport = LogTransport()
        engine = PushEngine(db_path=str(db_path), transport=transport, server_id="replay")
        config = PushSection(delivery_enabled=True)
        engine.push_config = config

        start_time = time.time()
        for i, msg in enumerate(messages):
            delay = msg["delay_s"] / speed
            if delay > 0:
                await asyncio.sleep(delay)
            stamp_now(msg["payload"], start_time=start_time, clock=time.time)

            topic = msg["topic"]
            payload = msg["payload"]
            sent_before = len(transport.sent)

            if topic == "frigate/events":
                await engine.handle_object_payload(payload)
            else:
                await engine.handle_review_payload(payload)

            new_sends = transport.sent[sent_before:]
            card_sends = [s for s in new_sends if "payload" in s and not s.get("live_activity")]
            la_sends = [s for s in new_sends if s.get("live_activity")]

            step_decision: dict[str, Any] = {
                "step": i + 1,
                "topic": topic,
                "type": payload["type"],
            }

            if card_sends:
                p = card_sends[0]["payload"]
                step_decision["mutation"] = p.get("mutation", "")
                step_decision["level"] = p.get("level", "")
                step_decision["sounded"] = "sound" in p.get("aps", {})
                step_decision["interruption_level"] = p.get("aps", {}).get("interruption-level", "")
                step_decision["thread_id"] = p.get("aps", {}).get("thread-id", "")
                step_decision["category"] = p.get("aps", {}).get("category", "")
                if p.get("aps", {}).get("sound"):
                    step_decision["sound_name"] = p["aps"]["sound"]
            else:
                step_decision["mutation"] = "(no push)"

            if la_sends:
                la = la_sends[0]
                step_decision["la_action"] = la["event"]
                tok = la.get("token", "")
                step_decision["la_token_type"] = (
                    "push-to-start" if tok.startswith("pts") else "per-activity"
                )
            elif topic == "frigate/reviews":
                step_decision["la_action"] = "(none)"

            decisions.append(step_decision)
    return decisions


def print_decisions(decisions: list[dict[str, Any]]) -> None:
    for d in decisions:
        parts = [f"step {d['step']}: {d['topic'].split('/')[-1]}/{d['type']}"]
        parts.append(f"mutation={d['mutation']}")
        if d.get("level"):
            parts.append(f"level={d['level']}")
        if "sounded" in d:
            parts.append(f"sounded={'yes' if d['sounded'] else 'no'}")
        if d.get("sound_name"):
            parts.append(f"sound={d['sound_name']}")
        if d.get("interruption_level"):
            parts.append(f"interruption={d['interruption_level']}")
        if d.get("la_action"):
            la_part = f"LA={d['la_action']}"
            if d.get("la_token_type"):
                la_part += f" ({d['la_token_type']})"
            parts.append(la_part)
        if d.get("thread_id"):
            parts.append(f"thread-id={d['thread_id']}")
        if d.get("category"):
            parts.append(f"category={d['category']}")
        print("  ".join(parts))


# ---------------------------------------------------------------------------
# MQTT publisher
# ---------------------------------------------------------------------------

class _MqttPublisher:
    def __init__(
        self, *, host: str, port: int, client_id: str,
        username: str | None, password: str | None,
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay canned Frigate sequences through the card pipeline."
    )
    parser.add_argument(
        "--scenario",
        help="Scenario name (looked up in tools/replay-scenarios/<name>.json) "
        "or a path to a scenario JSON file.",
    )
    parser.add_argument("--camera", help="Override the scenario's default camera.")
    parser.add_argument("--label", help="Override the scenario's default object label.")
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Time-compression multiplier for inter-message delays (default 1.0 = real time).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run the card pipeline in-process (LogTransport) and print decisions. "
        "No MQTT broker needed.",
    )
    parser.add_argument("--list", action="store_true", help="List available card scenarios.")
    parser.add_argument("--mqtt-host", help="Override push.mqtt_host from config.")
    parser.add_argument("--mqtt-port", type=int, help="Override push.mqtt_port from config.")
    parser.add_argument("--config", help="Path to sidecar.yml.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.list:
        scenarios = list_scenarios()
        if not scenarios:
            print("no card-* scenarios found")
            return 0
        for s in scenarios:
            print(f"  {s['name']}")
            if s["description"]:
                print(f"    {s['description'][:100]}")
        return 0

    if not args.scenario:
        print("error: --scenario is required (use --list to see available)", file=sys.stderr)
        return 1

    try:
        scenario = load_scenario(resolve_scenario_path(args.scenario))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    camera = args.camera or scenario.get("camera") or "doorbell"
    messages = build_messages(scenario, camera=camera, label=args.label)

    if args.dry_run:
        decisions = asyncio.run(
            dry_run_scenario(messages, speed=args.speed, camera=camera)
        )
        print_decisions(decisions)
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
        print(f"published {len(messages)} messages to MQTT")
        print("watch decisions: journalctl -u frigate-sidecar -f --grep 'push: card'")
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
