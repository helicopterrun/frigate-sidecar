"""Structured event search (`GET /v1/events/search`).

Reads Frigate's `event` table directly (read-only, no sidecar join needed)
and supports the filter set a search UI needs: free-text `q` across
label/sub_label/zones, comma-separated camera/label/zone allowlists, a
sub_label substring match, a start_time window, a minimum score, and a
has_snapshot flag. `zones` is stored as a JSON array on the row (Frigate's own
storage shape), so zone matching goes through `json_each()` rather than a
plain LIKE/IN.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Query, Request

from frigate_sidecar.config import Settings
from frigate_sidecar.db import open_frigate_ro

router = APIRouter(prefix="/v1", tags=["search"])

_MAX_LIMIT = 200


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _query_search(
    *,
    db_path: Any,
    q: str,
    cameras: list[str],
    labels: list[str],
    zones: list[str],
    sub_label: str,
    after: float | None,
    before: float | None,
    min_score: float | None,
    has_snapshot: bool | None,
    limit: int,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    if cameras:
        placeholders = ",".join("?" for _ in cameras)
        where.append(f"e.camera IN ({placeholders})")
        params.extend(cameras)

    if labels:
        placeholders = ",".join("?" for _ in labels)
        where.append(f"e.label IN ({placeholders})")
        params.extend(labels)

    if zones:
        placeholders = ",".join("?" for _ in zones)
        where.append(
            "EXISTS (SELECT 1 FROM json_each(e.zones) WHERE json_each.value IN "
            f"({placeholders}))"
        )
        params.extend(zones)

    if sub_label:
        where.append("e.sub_label LIKE ?")
        params.append(f"%{sub_label}%")

    if after is not None:
        where.append("e.start_time >= ?")
        params.append(after)

    if before is not None:
        where.append("e.start_time <= ?")
        params.append(before)

    if min_score is not None:
        where.append("JSON_EXTRACT(e.data, '$.score') >= ?")
        params.append(min_score)

    if has_snapshot is not None:
        where.append("e.has_snapshot = ?")
        params.append(1 if has_snapshot else 0)

    tokens = q.split()
    for token in tokens:
        like = f"%{token}%"
        where.append(
            "(e.label LIKE ? OR e.sub_label LIKE ? OR EXISTS "
            "(SELECT 1 FROM json_each(e.zones) WHERE json_each.value LIKE ?))"
        )
        params.extend([like, like, like])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit), _MAX_LIMIT))

    sql = f"""
        SELECT e.id, e.camera, e.label, e.sub_label, e.zones,
               e.start_time, e.end_time, e.has_clip, e.has_snapshot, e.data
        FROM event e
        {where_sql}
        ORDER BY e.start_time DESC
        LIMIT ?
    """
    params.append(limit)

    conn = open_frigate_ro(db_path)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "camera": row["camera"],
                "label": row["label"],
                "sub_label": row["sub_label"],
                "zones": json.loads(row["zones"]) if row["zones"] else [],
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "has_clip": bool(row["has_clip"]),
                "has_snapshot": bool(row["has_snapshot"]),
                "data": json.loads(row["data"]) if row["data"] else {},
                "search_distance": None,
                "search_source": "structured",
            }
        )
    return out


@router.get("/events/search")
async def events_search(
    request: Request,
    q: str = Query(default=""),
    cameras: str = Query(default=""),
    labels: str = Query(default=""),
    zones: str = Query(default=""),
    sub_label: str = Query(default=""),
    after: float | None = Query(default=None),
    before: float | None = Query(default=None),
    min_score: float | None = Query(default=None),
    has_snapshot: bool | None = Query(default=None),
    limit: int = Query(default=50),
) -> list[dict[str, Any]]:
    settings: Settings = request.app.state.settings
    return await asyncio.to_thread(
        _query_search,
        db_path=settings.frigate.db_path,
        q=q,
        cameras=_split_csv(cameras),
        labels=_split_csv(labels),
        zones=_split_csv(zones),
        sub_label=sub_label,
        after=after,
        before=before,
        min_score=min_score,
        has_snapshot=has_snapshot,
        limit=limit,
    )
