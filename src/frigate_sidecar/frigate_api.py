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


class FrigatePlusError(RuntimeError):
    """A Plus-submission call returned a non-200 success=False response."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FrigateClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        # Plus uploads can be slow (snapshot read + remote POST to plus.frigate.video),
        # so allow a longer timeout for those specifically.
        self._client = httpx.Client(timeout=timeout)
        self._plus_client = httpx.Client(timeout=30.0)

    def __enter__(self) -> FrigateClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._plus_client.close()

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

    def plus_enabled(self) -> bool:
        """Best-effort: returns True if Frigate reports a configured Plus API key.

        Reads `config.plus.enabled` from `/api/config`. Returns False if the
        config can't be fetched (Frigate down, network error) so the UI hides
        the Plus controls gracefully instead of showing a button that errors.
        """
        try:
            return bool(self.config().get("plus", {}).get("enabled"))
        except FrigateAPIError:
            return False

    def submit_plus(self, event_id: str, *, include_annotation: bool = True) -> str:
        """Submit an event to Frigate+ as a true positive. Returns the plus_id.

        `include_annotation=True` also uploads the bounding box with the event's
        label, so the user doesn't have to draw it on the Plus side. Raises
        FrigatePlusError on any non-success response.
        """
        url = f"{self.base_url}/api/events/{event_id}/plus"
        body: dict[str, Any] = {}
        if include_annotation:
            body["include_annotation"] = 1
        try:
            r = self._plus_client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise FrigatePlusError(f"POST {url}: {exc}", status_code=0) from exc
        return _parse_plus_response(r, "submit_plus")

    def submit_false_positive(self, event_id: str) -> str:
        """Submit an event as a false positive. Returns the plus_id.

        Frigate's endpoint is PUT (not POST) and internally calls /plus first
        if the event hasn't been submitted yet, so we don't need a two-step
        dance on this side.
        """
        url = f"{self.base_url}/api/events/{event_id}/false_positive"
        try:
            r = self._plus_client.put(url)
        except httpx.HTTPError as exc:
            raise FrigatePlusError(f"PUT {url}: {exc}", status_code=0) from exc
        return _parse_plus_response(r, "submit_false_positive")


def _parse_plus_response(r: httpx.Response, op: str) -> str:
    try:
        payload = r.json()
    except ValueError:
        payload = {}
    if r.status_code == 200 and payload.get("success"):
        plus_id = payload.get("plus_id")
        if not plus_id:
            raise FrigatePlusError(f"{op}: 200 OK but no plus_id in response", r.status_code)
        return str(plus_id)
    msg = payload.get("message") or f"HTTP {r.status_code}"
    raise FrigatePlusError(f"{op}: {msg}", r.status_code)
