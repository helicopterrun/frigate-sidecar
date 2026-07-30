"""Manual promote / discard decisions for face training crops.

`promote` pushes a train-bucket crop into a named Face Library via Frigate's API
(which moves the file out of train/); `discard` is a soft decision recorded only
in the sidecar DB — the crop stays in train/ until Frigate's rolling
`save_attempts` pool evicts it, so we never write into Frigate's clips dir.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from frigate_sidecar.config import Settings
from frigate_sidecar.db import open_sidecar
from frigate_sidecar.frigate_api import FrigateAPIError, FrigateClient


class FacePromoteError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_decision(
    sidecar_db: str | Path,
    filename: str,
    decision: str,
    assigned_name: str | None,
) -> None:
    conn = open_sidecar(sidecar_db)
    try:
        conn.execute(
            """
            UPDATE face_attempts
               SET decision = ?, assigned_name = ?, decided_at = ?
             WHERE filename = ?
            """,
            (decision, assigned_name, _now(), filename),
        )
        conn.commit()
    finally:
        conn.close()


def promote(settings: Settings, filename: str, name: str) -> dict[str, str]:
    """Promote a train crop into the `name` library, then record the decision.

    The name ends up both in an upstream URL path and in a directory Frigate
    creates under its face library, so path separators and traversal segments
    are rejected outright rather than relying on escaping alone.
    """
    name = name.strip()
    if not name:
        raise FacePromoteError("a non-empty name is required to promote")
    if any(c in name for c in "/\\") or ".." in name or any(ord(c) < 32 for c in name):
        raise FacePromoteError(f"invalid library name: {name!r}")
    if any(c in filename for c in "/\\") or ".." in filename:
        raise FacePromoteError(f"invalid training filename: {filename!r}")
    try:
        with FrigateClient(settings.frigate.base_url) as fc:
            fc.train_face(name, filename)
    except FrigateAPIError as exc:
        raise FacePromoteError(str(exc)) from exc
    _set_decision(settings.sidecar.db_path, filename, "promoted", name)
    return {"filename": filename, "decision": "promoted", "name": name}


def discard(settings: Settings, filename: str) -> dict[str, str]:
    """Mark a crop discarded (soft — DB only)."""
    _set_decision(settings.sidecar.db_path, filename, "discarded", None)
    return {"filename": filename, "decision": "discarded"}
