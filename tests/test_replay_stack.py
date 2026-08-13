"""Tests for tools/replay_stack.py."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))

_spec = importlib.util.spec_from_file_location(
    "replay_stack", TOOLS_DIR / "replay_stack.py"
)
assert _spec is not None and _spec.loader is not None
replay_stack = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay_stack)


class FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class FakeProcess:
    def __init__(self, *, lines: list[bytes], returncode: int) -> None:
        self.stdout = FakeStream(lines)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


def test_build_launch_plan_ordering_and_stagger():
    plan = replay_stack.build_launch_plan(["a", "b", "c"], 8.0)
    assert plan == [
        {"name": "a", "delay_s": 0.0},
        {"name": "b", "delay_s": 8.0},
        {"name": "c", "delay_s": 16.0},
    ]


def test_validate_scenario_names_accepts_known():
    replay_stack.validate_scenario_names(["card-la-person-doors", "card-la-package"])


def test_validate_scenario_names_rejects_unknown():
    with pytest.raises(ValueError, match="unknown scenario"):
        replay_stack.validate_scenario_names(["card-la-package", "not-a-scenario"])


def test_validate_scenario_names_lists_valid_names_in_error():
    with pytest.raises(ValueError, match="card-notify-resolve"):
        replay_stack.validate_scenario_names(["nope"])


def test_format_timestamp():
    import datetime

    dt = datetime.datetime.fromtimestamp(1_700_000_000.1234)
    expected = f"{dt:%H:%M:%S}.{dt.microsecond // 1000:03d}"
    assert replay_stack.format_timestamp(1_700_000_000.1234) == expected


def test_build_child_args_with_config():
    args = replay_stack.build_child_args("card-la-package", speed=2.0, config="config/sidecar.yml")
    assert args == [
        "--scenario", "card-la-package", "--speed", "2.0",
        "--config", "config/sidecar.yml",
    ]


def test_build_child_args_without_config():
    args = replay_stack.build_child_args("card-la-package", speed=1.0, config=None)
    assert args == ["--scenario", "card-la-package", "--speed", "1.0"]


async def test_run_stack_dry_run_prints_plan_without_spawning(capsys):
    async def spawn_should_not_be_called(name: str, args: list[str]) -> Any:
        raise AssertionError("spawn should not be called in --dry-run")

    exit_code = await replay_stack.run_stack(
        ["card-la-person-doors", "card-la-package"],
        stagger=8.0, speed=1.0, config="config/sidecar.yml",
        dry_run=True, spawn=spawn_should_not_be_called,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "card-la-person-doors" in out
    assert "card-la-package" in out
    assert "+8.0s" in out


async def test_run_stack_launches_in_order_with_stagger():
    spawned: list[tuple[str, list[str]]] = []
    slept: list[float] = []

    async def fake_spawn(name: str, args: list[str]) -> FakeProcess:
        spawned.append((name, args))
        return FakeProcess(lines=[f"{name} line\n".encode()], returncode=0)

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    exit_code = await replay_stack.run_stack(
        ["card-la-person-doors", "card-la-package"],
        stagger=8.0, speed=1.0, config=None,
        spawn=fake_spawn, sleep=fake_sleep, now=lambda: 1_700_000_000.0,
    )
    assert exit_code == 0
    assert [name for name, _ in spawned] == ["card-la-person-doors", "card-la-package"]
    # stagger applied once, between the two launches (not before the first)
    assert slept == [8.0]


async def test_run_stack_streams_prefixed_timestamped_output(capsys):
    async def fake_spawn(name: str, args: list[str]) -> FakeProcess:
        return FakeProcess(lines=[b"hello\n"], returncode=0)

    async def fake_sleep(seconds: float) -> None:
        pass

    exit_code = await replay_stack.run_stack(
        ["card-la-person-doors", "card-la-package"],
        stagger=8.0, speed=1.0, config=None,
        spawn=fake_spawn, sleep=fake_sleep, now=lambda: 1_700_000_000.0,
    )
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "[card-la-person-doors]" in out
    assert "[card-la-package]" in out
    assert replay_stack.format_timestamp(1_700_000_000.0) in out


async def test_run_stack_propagates_nonzero_when_child_fails(capsys):
    async def fake_spawn(name: str, args: list[str]) -> FakeProcess:
        returncode = 1 if name == "card-la-package" else 0
        return FakeProcess(lines=[], returncode=returncode)

    async def fake_sleep(seconds: float) -> None:
        pass

    exit_code = await replay_stack.run_stack(
        ["card-la-person-doors", "card-la-package"],
        stagger=8.0, speed=1.0, config=None,
        spawn=fake_spawn, sleep=fake_sleep,
    )
    assert exit_code == 1
    assert "card-la-package" in capsys.readouterr().err


def test_cli_requires_at_least_two_scenarios(capsys):
    exit_code = replay_stack.main(["--scenarios", "card-la-package"])
    assert exit_code == 1
    assert "2 or more" in capsys.readouterr().err


def test_cli_unknown_scenario_errors(capsys):
    exit_code = replay_stack.main([
        "--scenarios", "card-la-package", "not-a-scenario", "--dry-run",
    ])
    assert exit_code == 1
    assert "not-a-scenario" in capsys.readouterr().err


def test_cli_dry_run_success(capsys):
    exit_code = replay_stack.main([
        "--scenarios", "card-la-person-doors", "card-la-package",
        "--stagger", "8", "--dry-run",
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "card-la-person-doors" in out
    assert "card-la-package" in out
