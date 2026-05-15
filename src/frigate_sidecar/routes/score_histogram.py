"""Score-histogram page: per (camera, label) score distribution + suggestions."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from frigate_sidecar.analysis import score_histogram
from frigate_sidecar.db import open_joined

router = APIRouter(tags=["score-histogram"])


def _event_filter_options(frigate_db: str, sidecar_db: str) -> dict[str, list[str]]:
    """Distinct camera/label values from the event table for dropdowns."""
    conn = open_joined(frigate_db, sidecar_db, sidecar_alias="sidecar")
    try:
        cam_q = "SELECT DISTINCT camera FROM event ORDER BY camera"
        lbl_q = "SELECT DISTINCT label FROM event ORDER BY label"
        cams = [r["camera"] for r in conn.execute(cam_q)]
        labels = [r["label"] for r in conn.execute(lbl_q)]
    finally:
        conn.close()
    return {"cameras": cams, "labels": labels}


def _confidence_css(confidence: str) -> str:
    return {
        "high": "ok",
        "med": "warn",
        "low": "noise",
        "sparse": "muted",
    }.get(confidence, "muted")


@router.get("/score-histogram", response_class=HTMLResponse)
def score_histogram_view(
    request: Request,
    days: int = Query(default=14, ge=1, le=365),
    camera: str = "",
    label: str = "",
    min_samples: int = Query(default=30, ge=1),
) -> Any:
    settings = request.app.state.settings
    templates = request.app.state.templates

    result = score_histogram.analyze(
        frigate_db=settings.frigate.db_path,
        sidecar_db=settings.sidecar.db_path,
        days=days,
        camera=camera or None,
        label=label or None,
        min_samples=min_samples,
    )
    # Attach a css class per row based on confidence.
    for row in result["rows"]:
        row["css"] = _confidence_css(row["confidence"])

    options = _event_filter_options(
        str(settings.frigate.db_path), str(settings.sidecar.db_path)
    )

    return templates.TemplateResponse(
        request,
        "score_histogram.html",
        {
            "result": result,
            "filters": {
                "days": days,
                "camera": camera,
                "label": label,
                "min_samples": min_samples,
            },
            "cameras": options["cameras"],
            "labels": options["labels"],
            "counts": {},
        },
    )
