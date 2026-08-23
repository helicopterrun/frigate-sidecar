"""Identity clusters from face enrichment: list, name, merge, delete.

The page's data is `face_clusters` + `face_enrichments` (faces/enrich.py).
Naming a cluster is the promotion path — it becomes a KNOWN person and later
matches write the event's sub_label back to Frigate. Naming and merging both
rebuild the centroid exactly from the stored per-event embeddings rather than
trusting the running mean.

Thumbnails come from Frigate's event thumbnail endpoint, proxied server-side:
the sidecar's base_url origin is unauthenticated and server-to-server only, so
the browser can never be pointed at it directly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, cast

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from frigate_sidecar.config import Settings
from frigate_sidecar.db import open_sidecar
from frigate_sidecar.faces import enrich
from frigate_sidecar.frigate_api import get_async_client

router = APIRouter(tags=["enrich"])


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _clusters(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT c.cluster_id, c.name, c.observation_count, c.created_at, c.last_seen_at, "
            "       (SELECT fe.event_id FROM face_enrichments fe "
            "         WHERE fe.cluster_id = c.cluster_id "
            "         ORDER BY fe.best_quality DESC LIMIT 1) AS sample_event_id "
            "  FROM face_clusters c ORDER BY (c.name IS NULL), c.last_seen_at DESC"
        )
    ]
    for r in rows:
        ts = float(r.get("last_seen_at") or 0.0)
        r["last_seen"] = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
    return rows


@router.get("/enrich/clusters", response_class=HTMLResponse)
def clusters_view(request: Request) -> Any:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        clusters = _clusters(conn)
    finally:
        conn.close()
    return _templates(request).TemplateResponse(
        request,
        "enrich.html",
        {
            "clusters": clusters,
            "enabled": s.face_enrich.enabled,
            "cameras": s.face_enrich.cameras,
        },
    )


@router.get("/enrich/clusters.json")
def clusters_json(request: Request) -> JSONResponse:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        return JSONResponse({"clusters": _clusters(conn)})
    finally:
        conn.close()


@router.get("/enrich/thumb/{event_id}")
async def cluster_thumb(event_id: str, request: Request) -> Response:
    """Proxy Frigate's event thumbnail for a cluster's best sample event."""
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM face_enrichments WHERE event_id = ?", (event_id,)
        ).fetchone()
    finally:
        conn.close()
    # Only ids we enriched are proxied — the path param never becomes a free
    # fetch against the unauthenticated origin.
    if row is None:
        raise HTTPException(status_code=404, detail="unknown event")
    client = get_async_client(request.app)
    url = f"{s.frigate.base_url}/api/events/{event_id}/thumbnail.jpg"
    try:
        r = await client.get(url, timeout=10.0)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="frigate unreachable") from exc
    if r.status_code != 200:
        raise HTTPException(status_code=404, detail="no thumbnail")
    return Response(content=r.content, media_type="image/jpeg")


class NamePayload(BaseModel):
    name: str


@router.post("/enrich/clusters/{cluster_id}/name")
def cluster_name(cluster_id: int, payload: NamePayload, request: Request) -> JSONResponse:
    name = payload.name.strip()
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="name must be 1-100 chars")
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        cur = conn.execute(
            "UPDATE face_clusters SET name = ? WHERE cluster_id = ?", (name, cluster_id)
        )
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="cluster not found")
        enrich.rebuild_centroid(conn, cluster_id)
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


class MergePayload(BaseModel):
    into: int  # surviving cluster_id


@router.post("/enrich/clusters/{cluster_id}/merge")
def cluster_merge(cluster_id: int, payload: MergePayload, request: Request) -> JSONResponse:
    if payload.into == cluster_id:
        raise HTTPException(status_code=400, detail="cannot merge a cluster into itself")
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        for cid in (cluster_id, payload.into):
            if not conn.execute(
                "SELECT 1 FROM face_clusters WHERE cluster_id = ?", (cid,)
            ).fetchone():
                raise HTTPException(status_code=404, detail=f"cluster {cid} not found")
        conn.execute(
            "UPDATE face_enrichments SET cluster_id = ? WHERE cluster_id = ?",
            (payload.into, cluster_id),
        )
        conn.execute("DELETE FROM face_clusters WHERE cluster_id = ?", (cluster_id,))
        enrich.rebuild_centroid(conn, payload.into)
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


@router.post("/enrich/clusters/{cluster_id}/delete")
def cluster_delete(cluster_id: int, request: Request) -> JSONResponse:
    s = _settings(request)
    conn = open_sidecar(s.sidecar.db_path)
    try:
        conn.execute(
            "UPDATE face_enrichments SET cluster_id = NULL, embedding = NULL "
            "WHERE cluster_id = ?",
            (cluster_id,),
        )
        cur = conn.execute("DELETE FROM face_clusters WHERE cluster_id = ?", (cluster_id,))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="cluster not found")
        return JSONResponse({"ok": True})
    finally:
        conn.close()
