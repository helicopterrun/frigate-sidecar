"""FastAPI app factory and uvicorn entry."""

from __future__ import annotations

from fastapi import FastAPI

from frigate_sidecar import __version__
from frigate_sidecar.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="frigate-sidecar",
        version=__version__,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/version", tags=["meta"])
    def version() -> dict[str, str]:
        return {"version": __version__}

    return app


def run() -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.sidecar.bind_host,
        port=settings.sidecar.bind_port,
        log_level=settings.log_level.lower(),
    )
