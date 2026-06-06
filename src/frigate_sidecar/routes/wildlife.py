"""Wildlife-cam gallery: a viewer for the PoE trail-camera on the Pi.

Deliberately unrelated to Frigate analysis (like `toybox`) — it surfaces the
stills produced by a separate project (`helicopterrun/wildlife-cam`, a FastAPI
backend on a Raspberry Pi at 192.168.1.37:8000). We do NOT talk to that backend
from Python here: the page is fully client-side and fetches the wildlife API
through a same-origin reverse-proxy prefix (default ``/wildlifecam/``) configured
in Nginx Proxy Manager. NPM injects the ``X-API-Token`` for the mutating control
endpoints, so no token ever ships in our JS.

This route only renders the shell; ``static/js/wildlife.js`` does the polling.
See the wildlife-cam repo's ``docs/API.md`` for the consumed contract.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["wildlife"])

# Same-origin prefix the page calls the wildlife API under. The NPM reverse
# proxy maps this to the Pi (http://192.168.1.37:8000/) and injects the API
# token. Override per-request with ?api=<base> for LAN-direct testing (read
# endpoints are open; controls need the proxy's token injection).
_API_BASE = "/wildlifecam"


@router.get("/wildlife", response_class=HTMLResponse)
def wildlife_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "wildlife.html",
        {"api_base": _API_BASE, "counts": {}},
    )
