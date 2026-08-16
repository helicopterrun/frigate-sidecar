"""Login page: a browser hitting a gated sidecar page without a live Frigate
session is redirected here (`auth.FrigateAuthMiddleware`). The form posts the
credentials directly to Frigate's own `/api/login` *through the sidecar's
reverse proxy*, so Frigate's `Set-Cookie: frigate_token=...` lands on the
sidecar's origin and every gated page works from then on. The sidecar itself
never sees or stores the password — same trust model as the rest of
`auth.py`."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {})
