"""Wildlife-cam gallery: a viewer for the PoE trail-camera on the Pi.

Deliberately unrelated to Frigate analysis (like `toybox`) — it surfaces the
stills and PIR motion events produced by a separate project
(`helicopterrun/wildlife-cam`, a FastAPI backend on a Raspberry Pi at
192.168.1.37:8000).

The page is otherwise fully client-side: it fetches the wildlife API through a
same-origin reverse-proxy prefix (default ``/wildlifecam/``) configured in Nginx
Proxy Manager, which injects the ``X-API-Token`` for the mutating control
endpoints, so no token ever ships in our JS.

The ONE server-side exception is event-clip playback (``wildlife_media`` below).
Day events carry an ``.mp4`` under the Pi's open ``/media/{path}`` endpoint, but
that path isn't reliably forwarded by the NPM ``/wildlifecam`` location, so the
``<video>`` would 404. We stream those clips through the sidecar instead — media
is an open read endpoint upstream, so no token is involved. ``static/js/
wildlife.js`` does everything else. See the wildlife-cam repo's ``docs/API.md``
for the consumed contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

router = APIRouter(tags=["wildlife"])

# Browser-facing prefix (NPM → Pi, with token injection) used by the page JS.
# Override per-request with ?api=<base> for LAN-direct testing (read endpoints
# are open; controls need the proxy's token injection).
_API_BASE = "/wildlifecam"

# Direct LAN address of the wildlife-cam backend, used ONLY for the server-side
# clip proxy below. Hardcoded like the rest of this integration (the Pi has a
# static lease and no Tailscale/DNS name that resolves from the LXC).
_PI_BASE = "http://192.168.1.37:8000"

# Request headers forwarded upstream (range → <video> seeking) and response
# headers passed back (content metadata the player needs).
_REQ_PASS = ("range",)
_RESP_PASS = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
)

# Recording-segment poster extraction (ffmpeg). Segments are sideways cam H.264
# with no thumbnail of their own; we grab + rotate + cache the first frame.
_FFMPEG = "ffmpeg"
_POSTER_W = 480  # output width; height auto (keeps aspect)
_POSTER_TIMEOUT_S = 20.0
# Posters are immutable and only the most recent segments are ever browsed, so
# old entries become dead weight. Bound the cache (~30 MB at ~15 KB each),
# evicting oldest-by-mtime on extraction.
_POSTER_CACHE_MAX = 2000
# A 60-tile grid that all-missed the cache would otherwise spawn 60 ffmpegs at
# once — cap it (each extraction is ~0.2s, so a small pool drains fast).
_ffmpeg_sem = asyncio.Semaphore(3)


@router.get("/wildlife", response_class=HTMLResponse)
def wildlife_view(request: Request) -> Any:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "wildlife.html",
        {"api_base": _API_BASE, "counts": {}},
    )


@router.get("/wildlife/media/{path:path}")
async def wildlife_media(path: str, request: Request) -> Any:
    """Stream a clip/media file from the Pi's open ``/media/{path}`` endpoint.

    Routed through the sidecar so ``<video>`` playback doesn't depend on the NPM
    ``/wildlifecam/media`` forwarding. ``Range`` passes through so the browser
    can seek, and the upstream status (200 / 206 / 404) is mirrored.
    """
    # Defense-in-depth: the {path:path} capture could contain traversal. The Pi
    # resolves it under its own /media root regardless, but reject it here too.
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="bad path")

    upstream = f"{_PI_BASE}/media/{path}"
    fwd = {k: v for k, v in request.headers.items() if k.lower() in _REQ_PASS}

    # No read timeout — we're streaming a video the user may pause/seek.
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        req = client.build_request("GET", upstream, headers=fwd)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    if resp.status_code >= 400:
        code = resp.status_code
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=code, detail="clip unavailable")

    headers = {k: resp.headers[k] for k in _RESP_PASS if k in resp.headers}

    async def body() -> Any:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=resp.status_code,
        headers=headers,
        media_type=resp.headers.get("content-type"),
    )


def _poster_cache_dir(request: Request) -> Path:
    # Live alongside the sidecar DB (follows wherever the data dir is mounted).
    d = request.app.state.settings.sidecar.db_path.parent / "wildlife-posters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_poster_cache(cache_dir: Path) -> None:
    """Keep the cache under _POSTER_CACHE_MAX, dropping oldest-by-mtime first."""
    try:
        jpgs = sorted(cache_dir.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in jpgs[: max(0, len(jpgs) - _POSTER_CACHE_MAX)]:
        try:
            p.unlink()
        except OSError:
            pass


async def _extract_poster(src_url: str, dst: Path) -> bool:
    """Pull the first frame of a segment, rotate it upright, scale, write to dst.

    transpose=2 = 90° CCW, matching the player's CSS ``rotate(-90deg)`` so the
    poster and the played video share orientation. Returns False on any failure.
    """
    cmd = [
        _FFMPEG, "-nostdin", "-loglevel", "error",
        "-i", src_url,
        "-frames:v", "1",
        "-vf", f"transpose=2,scale={_POSTER_W}:-1",
        "-q:v", "4",
        "-y", str(dst),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_POSTER_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False
    except OSError:
        return False
    return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 0


@router.get("/wildlife/poster/{path:path}")
async def wildlife_poster(path: str, request: Request) -> Any:
    """First-frame JPEG poster for a recording segment, extracted with ffmpeg.

    Recording segments (sideways ``cam`` H.264) have no thumbnail of their own,
    so a recordings grid is otherwise just timestamps. This grabs the segment's
    own first frame from the Pi, rotates it upright, scales it down, and caches
    it — always the correct camera/frame, unlike a time-matched snapshot. Posters
    are immutable per segment, so cache hits serve straight from disk.

    ``path`` is the segment's ``/media``-relative path (e.g. ``cam/seg_x.mp4``).
    """
    if ".." in path.split("/"):
        raise HTTPException(status_code=400, detail="bad path")

    cache_dir = _poster_cache_dir(request)
    key = hashlib.sha256(path.encode("utf-8")).hexdigest()[:20]
    out = cache_dir / f"{key}.jpg"

    if not out.exists():
        async with _ffmpeg_sem:
            # Double-checked: another request may have produced it while we
            # waited for the semaphore.
            if not out.exists():
                fd, tmp_name = tempfile.mkstemp(dir=cache_dir, suffix=".tmp.jpg")
                tmp = Path(tmp_name)
                os.close(fd)
                ok = await _extract_poster(f"{_PI_BASE}/media/{path}", tmp)
                if not ok:
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(status_code=502, detail="poster extraction failed")
                tmp.replace(out)  # atomic publish
                _prune_poster_cache(cache_dir)

    return FileResponse(
        out,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
