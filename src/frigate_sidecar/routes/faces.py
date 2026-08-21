"""Face-crop curation: review grid, histogram, crop images, promote/discard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from frigate_sidecar.config import Settings
from frigate_sidecar.db import open_sidecar
from frigate_sidecar.faces import scorer
from frigate_sidecar.faces.promoter import FacePromoteError, discard, promote
from frigate_sidecar.frigate_api import FrigateAPIError, FrigateClient

router = APIRouter(tags=["faces"])

_IMAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg")
_MEDIA_TYPES = {
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _templates(request: Request) -> Jinja2Templates:
    return cast(Jinja2Templates, request.app.state.templates)


def _libraries(settings: Settings) -> list[str]:
    try:
        with FrigateClient(settings.frigate.base_url) as fc:
            faces = fc.get_faces()
        return sorted(n for n in faces if n != "train")
    except FrigateAPIError:
        return []


def _pending(settings: Settings, limit: int = 200) -> list[dict[str, Any]]:
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        rows = conn.execute(
            """
            SELECT filename, event_id, recognized_name, recog_score,
                   sharpness, area_px, quality_score
              FROM face_attempts
             WHERE decision = 'pending'
             ORDER BY quality_score DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        name = r["recognized_name"] or "unknown"
        out.append(
            {
                "filename": r["filename"],
                "event_id": r["event_id"],
                "recognized_name": name,
                "is_recognized": name != "unknown",
                "recog_score": r["recog_score"],
                "sharpness": r["sharpness"],
                "area_px": r["area_px"],
                "quality_score": r["quality_score"],
                "suggested_name": "" if name == "unknown" else name,
            }
        )
    return out


@router.get("/faces", response_class=HTMLResponse)
def faces_view(request: Request) -> Any:
    s = _settings(request)
    crops = _pending(s)
    hist = scorer.histogram(s)
    return _templates(request).TemplateResponse(
        request,
        "faces.html",
        {
            "crops": crops,
            "hist": hist,
            "libraries": _libraries(s),
            "auto_promote": s.face.auto_promote,
            "quality_threshold": s.face.quality_threshold,
        },
    )


@router.get("/faces/histogram")
def faces_histogram(request: Request) -> JSONResponse:
    return JSONResponse(scorer.histogram(_settings(request)))


@router.get("/faces/crop/{filename}")
def faces_crop(filename: str, request: Request) -> FileResponse:
    """Serve a single train-bucket crop image from the configured faces dir."""
    # Reject any path traversal — we only ever serve flat files from train/.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    if Path(filename).suffix.lower() not in _IMAGE_EXTS:
        raise HTTPException(status_code=400, detail="not an image")
    s = _settings(request)
    path = Path(s.face.clips_faces_dir) / "train" / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="crop not found")
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


class DecidePayload(BaseModel):
    filename: str
    action: str  # 'promote' | 'discard'
    name: str | None = None


@router.post("/faces/decide")
def faces_decide(payload: DecidePayload, request: Request) -> JSONResponse:
    s = _settings(request)
    if payload.action == "discard":
        return JSONResponse({"ok": True, **discard(s, payload.filename)})
    if payload.action == "promote":
        if not (payload.name or "").strip():
            raise HTTPException(status_code=400, detail="name required to promote")
        try:
            result = promote(s, payload.filename, payload.name)  # type: ignore[arg-type]
        except FacePromoteError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return JSONResponse({"ok": True, **result})
    raise HTTPException(status_code=400, detail="action must be 'promote' or 'discard'")


@router.post("/faces/scan")
def faces_scan(request: Request) -> JSONResponse:
    s = _settings(request)
    try:
        summary = scorer.scan(s)
    except scorer.FacesUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "summary": summary})
