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
