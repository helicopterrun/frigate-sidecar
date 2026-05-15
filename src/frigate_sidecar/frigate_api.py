"""Thin client over Frigate's HTTP API.

The analysis modules that need live signal (detector inference times,
per-camera observed fps, recordings summaries) go through here so there's
one place to set timeouts / headers / base URL.
"""

from __future__ import annotations

from typing import Any

import httpx


class FrigateAPIError(RuntimeError):
    pass


class FrigateClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def __enter__(self) -> FrigateClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get_json(self, path: str) -> Any:
        url = f"{self.base_url}{path}"
        try:
            r = self._client.get(url)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise FrigateAPIError(f"GET {url}: {exc}") from exc
        return r.json()

    def stats(self) -> dict[str, Any]:
        return self._get_json("/api/stats")

    def config(self) -> dict[str, Any]:
        return self._get_json("/api/config")

    def recordings_summary(self, camera: str) -> list[dict[str, Any]]:
        return self._get_json(f"/api/{camera}/recordings/summary")
