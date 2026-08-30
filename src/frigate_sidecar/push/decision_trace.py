"""In-memory ring buffer of routing decisions (spec §7).

Pre-fanout: one entry per event decision, not per device. The buffer is
bounded (500 entries), memory-only, and losing it on restart is acceptable.
Append is O(1) and never raises — it must not interfere with the push path.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

_BUFFER_CAP = 500
_SERVE_CAP = 200

_lock = threading.Lock()
_buffer: deque[dict[str, Any]] = deque(maxlen=_BUFFER_CAP)
_counter = 0


def append(
    *,
    camera: str,
    label: str,
    subject: str,
    zones: list[str],
    place: str,
    level: str,
    reasons: list[str],
    event_id: str,
) -> dict[str, Any]:
    """Append a decision entry. Returns the entry for testing convenience."""
    global _counter
    with _lock:
        _counter += 1
        entry = {
            "id": f"dec-{_counter:08d}",
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "camera": camera,
            "label": label,
            "subject": subject,
            "zones": zones,
            "place": place,
            "level": level,
            "reasons": reasons,
            "event_id": event_id,
        }
        _buffer.append(entry)
    return entry


def annotate(
    event_id: str,
    *,
    family: str | None = None,
    la_started: bool | None = None,
    la_reason: str | None = None,
) -> None:
    """Patch the newest buffered entry for `event_id` with the Live Activity
    side of the decision (one alerts stack: the feed covers the whole
    stack, not just banner routing). The LA call site runs after `append`,
    so patching in place keeps one entry per decision. No-op when the entry
    has already rotated out -- like `append`, this must never raise into
    the push path."""
    with _lock:
        for entry in reversed(_buffer):
            if entry["event_id"] == event_id:
                if family is not None:
                    entry["family"] = family
                if la_started is not None:
                    entry["la_started"] = la_started
                if la_reason is not None:
                    entry["la_reason"] = la_reason
                return


def reasons_for(event_id: str) -> list[str]:
    """Best-effort `reasons` for the newest buffered entry matching
    `event_id`, or `[]` when it has already rotated out of the bounded ring
    (or was never recorded). Volatile by construction -- callers (e.g. the
    card-for-event route) must treat this as optional context, never as a
    durable record."""
    with _lock:
        for entry in reversed(_buffer):
            if entry["event_id"] == event_id:
                reasons = entry.get("reasons") or []
                return list(reasons)
    return []


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """Return up to `limit` most recent entries, newest first."""
    limit = max(1, min(limit, _SERVE_CAP))
    with _lock:
        items = list(_buffer)
    items.reverse()
    return items[:limit]


def reset_for_tests() -> None:
    """Clear all state — test isolation only."""
    global _counter
    with _lock:
        _buffer.clear()
        _counter = 0
