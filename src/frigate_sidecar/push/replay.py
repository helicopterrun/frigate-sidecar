"""Replay scenario core: importable by both the CLI and the web UI.

All scenario logic lives here. The CLI (`tools/replay_card.py`) is a thin
wrapper; the web handler (`routes/replay.py`) calls `start_run` to drive
scenarios through the sidecar's own MQTT connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from frigate_sidecar.config import PushSection

logger = logging.getLogger(__name__)

SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "tools" / "replay-scenarios"
REPLAY_ID_PREFIX = "replay-"


# ---------------------------------------------------------------------------
# Scenario discovery / loading / message building
# ---------------------------------------------------------------------------

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


def _inject_activity_token(db_path: str | Path, la_send: dict[str, Any]) -> None:
    """After a dry-run LA start, inject a per-activity token so subsequent
    update/end pushes have somewhere to go."""
    from frigate_sidecar import db as _db
    conn = _db.open_sidecar(db_path)
    try:
        rows = conn.execute(
            "SELECT activity_id FROM push_activities WHERE ended_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE push_activities SET token = ? WHERE activity_id = ?",
                (f"fake-activity-token-{row['activity_id']}", row["activity_id"]),
            )
        conn.commit()
    finally:
        conn.close()


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
                la_send = la_sends[0]
                step_decision["la_action"] = la_send["event"]
                # Where did the sound go? Under la_first the card is often
                # demoted and the LA start/escalation alert carries it — the
                # decision log must show that, or a silent card reads as a
                # dropped sound.
                la_alert = la_send.get("payload", {}).get("aps", {}).get("alert") or {}
                if la_alert.get("sound"):
                    step_decision["la_sound_name"] = la_alert["sound"]
                tok = la_send.get("token", "")
                step_decision["la_token_type"] = (
                    "push-to-start" if tok.startswith("pts") else "per-activity"
                )
                # Simulate the app uploading the per-activity token after
                # a successful start — without this, update/end pushes can
                # never be sent in dry-run mode.
                if la_send["event"] == "start":
                    _inject_activity_token(db_path, la_send)
            elif topic == "frigate/reviews":
                step_decision["la_action"] = "(none)"

            decisions.append(step_decision)
    return decisions


# ---------------------------------------------------------------------------
# MQTT publisher (used by both CLI and web handler)
# ---------------------------------------------------------------------------

class MqttPublisher:
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
# Web-driven run state
# ---------------------------------------------------------------------------

@dataclass
class ReplayRun:
    run_id: str
    scenarios: list[str]
    state: str = "queued"
    decisions: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    messages_sent: int = 0
    messages_total: int = 0
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenarios": self.scenarios,
            "state": self.state,
            "decisions": self.decisions,
            "error": self.error,
            "messages_sent": self.messages_sent,
            "messages_total": self.messages_total,
            "dry_run": self.dry_run,
        }


_current_run: ReplayRun | None = None
_run_lock = asyncio.Lock()


def get_current_run() -> ReplayRun | None:
    return _current_run


async def start_run(
    scenario_names: list[str],
    *,
    speed: float = 1.0,
    dry_run: bool = False,
    push_settings: PushSection | None = None,
    stagger: float = 8.0,
) -> ReplayRun:
    """Start a replay run. Blocks until complete."""
    global _current_run

    if _run_lock.locked():
        raise RuntimeError("a replay run is already in progress")

    run_id = uuid.uuid4().hex[:8]
    run = ReplayRun(
        run_id=run_id,
        scenarios=scenario_names,
        dry_run=dry_run,
    )
    _current_run = run
    await _execute_run(run, speed=speed, dry_run=dry_run,
                       push_settings=push_settings, stagger=stagger)
    return run


async def _execute_run(
    run: ReplayRun,
    *,
    speed: float,
    dry_run: bool,
    push_settings: PushSection | None,
    stagger: float,
) -> None:
    async with _run_lock:
        try:
            run.state = "running"
            all_messages: list[tuple[str, list[dict[str, Any]]]] = []
            for name in run.scenarios:
                scenario = load_scenario(resolve_scenario_path(name))
                msgs = build_messages(scenario, run_id=run.run_id)
                all_messages.append((name, msgs))
                run.messages_total += len(msgs)

            if dry_run:
                for i, (name, msgs) in enumerate(all_messages):
                    if i > 0:
                        await asyncio.sleep(stagger / speed)
                    decisions = await dry_run_scenario(msgs, speed=speed)
                    run.decisions.extend(decisions)
                    run.messages_sent += len(msgs)
            else:
                if push_settings is None:
                    raise RuntimeError("push_settings required for live run")
                publisher = MqttPublisher(
                    host=push_settings.mqtt_host,
                    port=push_settings.mqtt_port,
                    client_id=f"frigate-sidecar-replay-{run.run_id}",
                    username=push_settings.mqtt_username,
                    password=push_settings.mqtt_password,
                )
                try:
                    for i, (name, msgs) in enumerate(all_messages):
                        if i > 0:
                            await asyncio.sleep(stagger / speed)
                        start_time = time.time()
                        for msg in msgs:
                            delay = msg["delay_s"] / speed
                            if delay > 0:
                                await asyncio.sleep(delay)
                            stamp_now(msg["payload"], start_time=start_time, clock=time.time)
                            publisher(msg["topic"], json.dumps(msg["payload"]))
                            run.messages_sent += 1
                finally:
                    publisher.close()

            run.state = "done"
        except Exception as exc:
            run.state = "failed"
            run.error = str(exc)
            logger.exception("replay: run %s failed", run.run_id)
