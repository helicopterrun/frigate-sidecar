"""Score Frigate's auto-saved face crops and curate the training pool.

Frigate writes recognition *attempts* into `<faces>/train/` as webp crops named:

    <eventStart>-<eventRand>-<frameTs>-<name>-<recogScore>.webp

where `name` is the recognized person (hyphens replaced with `_`) or `unknown`,
and `recogScore` is *recognition* confidence — NOT image quality. We compute our
own quality signal (Laplacian-variance sharpness x decoded crop area), record it
in `face_attempts`, and auto-promote high-quality *recognized* crops into the
Face Library. Unknowns and low-quality crops stay `pending` for manual review.

cv2 + numpy are an optional dep (the `[faces]` / `[annotation]` extra); the entry
points raise `FacesUnavailable` with a clean message if they're missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from frigate_sidecar.config import Settings
from frigate_sidecar.db import open_sidecar
from frigate_sidecar.frigate_api import FrigateAPIError, FrigateClient

# Reference points for normalizing the two raw metrics into a 0..1 quality
# score. These are deliberately conservative defaults — the first observe-only
# scan + the histogram tell us where the real pool sits, then tune the
# auto-promote `quality_threshold` accordingly.
SHARPNESS_REF = 180.0  # Laplacian variance at which a crop counts as "sharp"
AREA_REF = 10000.0  # crop area (px) at which a face counts as "big enough" (~100x100)

_IMAGE_EXTS = (".webp", ".png", ".jpg", ".jpeg")
_TERMINAL = ("auto_promoted", "promoted", "discarded")


class FacesUnavailable(RuntimeError):
    """cv2 / numpy not installed."""


def _require_cv2() -> tuple[Any, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise FacesUnavailable(
            'cv2 / numpy not installed. Install with `pip install "frigate-sidecar[faces]"`.'
        ) from exc
    return cv2, np


@dataclass
class ParsedFace:
    event_id: str
    frame_ts: float
    name: str  # 'unknown' or recognized person name
    recog_score: float


def parse_filename(fn: str) -> ParsedFace | None:
    """Parse a train-bucket crop filename. Returns None if it doesn't match.

    The recognized name has its hyphens replaced with `_` by Frigate, so the
    only hyphen separators are the structural ones — we can split from the right.
    """
    stem = fn
    for ext in _IMAGE_EXTS:
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    else:
        return None
    try:
        rest, score_s = stem.rsplit("-", 1)
        rest2, name = rest.rsplit("-", 1)
        parts = rest2.split("-")
        if len(parts) != 3:
            return None
        event_id = f"{parts[0]}-{parts[1]}"
        return ParsedFace(
            event_id=event_id,
            frame_ts=float(parts[2]),
            name=name,
            recog_score=float(score_s),
        )
    except (ValueError, IndexError):
        return None


def score_crop(path: Path) -> tuple[float, int, float]:
    """Return (sharpness, area_px, quality_score) for a crop on disk.

    sharpness = variance of the Laplacian (focus measure); area_px = w*h of the
    decoded image; quality_score = geometric mean of the two normalized metrics,
    so a crop must be both reasonably sharp AND reasonably large to score high.
    """
    cv2, np = _require_cv2()
    img = cv2.imread(str(path))
    if img is None:
        return 0.0, 0, 0.0
    h, w = img.shape[:2]
    area = int(h * w)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    sharp_norm = min(1.0, sharpness / SHARPNESS_REF)
    area_norm = min(1.0, area / AREA_REF)
    quality = float((sharp_norm * area_norm) ** 0.5)
    return sharpness, area, quality


def resolve_library_name(recognized_name: str, libraries: set[str]) -> str | None:
    """Map a recognized name to an existing Face Library dir, or None.

    Frigate embeds whatever label was current *at attempt time*, which may be a
    full name (`Christopher Horton`) even though the library was later renamed to
    a first name (`Christopher`). We only auto-promote when the name resolves to
    exactly one existing library: exact match first, then a unique first-token
    match. Anything ambiguous falls through to manual review.
    """
    if not recognized_name or recognized_name.lower() == "unknown":
        return None
    lib_by_lower = {lib.lower(): lib for lib in libraries}
    exact = lib_by_lower.get(recognized_name.lower())
    if exact:
        return exact
    first_token = recognized_name.replace("_", " ").split(" ", 1)[0].lower()
    matches = [lib for low, lib in lib_by_lower.items() if low.split(" ", 1)[0] == first_token]
    return matches[0] if len(matches) == 1 else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def scan(settings: Settings) -> dict[str, int]:
    """Walk the train/ pool, score new crops, and (optionally) auto-promote.

    Idempotent: crops already recorded with a terminal decision are skipped, and
    upserts use ON CONFLICT so re-running never duplicates rows or double-promotes.
    Returns a summary dict suitable for CLI/HTTP display.
    """
    _require_cv2()  # fail fast with a clean message before touching the DB
    face = settings.face
    train_dir = Path(face.clips_faces_dir) / "train"
    summary = {
        "scanned": 0,
        "new": 0,
        "rescored": 0,
        "auto_promoted": 0,
        "would_auto_promote": 0,
        "skipped_unparseable": 0,
        "errors": 0,
    }
    if not train_dir.is_dir():
        return summary

    # Live library names + per-person counts gate name resolution and the cap.
    libraries: set[str] = set()
    lib_counts: dict[str, int] = {}
    try:
        with FrigateClient(settings.frigate.base_url) as fc:
            faces = fc.get_faces()
        for name, imgs in faces.items():
            if name == "train":
                continue
            libraries.add(name)
            lib_counts[name] = len(imgs) if isinstance(imgs, list) else 0
    except FrigateAPIError:
        # Frigate down: we can still score + record, just can't promote this run.
        pass

    # One client for the whole scan: a promote-heavy run used to build (and
    # tear down) a fresh connection pool per crop.
    promote_client = FrigateClient(settings.frigate.base_url) if face.auto_promote else None
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        existing = {
            r["filename"]: r["decision"]
            for r in conn.execute("SELECT filename, decision FROM face_attempts")
        }
        for path in sorted(train_dir.iterdir()):
            if not path.is_file() or path.suffix.lower() not in _IMAGE_EXTS:
                continue
            fn = path.name
            summary["scanned"] += 1
            if existing.get(fn) in _TERMINAL:
                continue
            parsed = parse_filename(fn)
            if parsed is None:
                summary["skipped_unparseable"] += 1
                continue
            try:
                sharpness, area, quality = score_crop(path)
            except Exception:  # noqa: BLE001 — a single bad crop must not abort the scan
                summary["errors"] += 1
                continue

            lib = resolve_library_name(parsed.name, libraries)
            eligible = (
                lib is not None
                and parsed.recog_score >= face.min_recog_score
                and quality >= face.quality_threshold
                and lib_counts.get(lib, 0) < face.per_person_cap
            )

            decision = "pending"
            decided_at: str | None = None
            if eligible and promote_client is not None and lib is not None:
                try:
                    promote_client.train_face(lib, fn)
                    decision = "auto_promoted"
                    decided_at = _now()
                    lib_counts[lib] = lib_counts.get(lib, 0) + 1
                    summary["auto_promoted"] += 1
                except FrigateAPIError:
                    summary["errors"] += 1
            elif eligible:
                summary["would_auto_promote"] += 1

            conn.execute(
                """
                INSERT INTO face_attempts(
                    filename, event_id, frame_ts, recognized_name, recog_score,
                    sharpness, area_px, quality_score, decision, assigned_name,
                    scored_at, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    event_id=excluded.event_id, frame_ts=excluded.frame_ts,
                    recognized_name=excluded.recognized_name,
                    recog_score=excluded.recog_score, sharpness=excluded.sharpness,
                    area_px=excluded.area_px, quality_score=excluded.quality_score,
                    decision=excluded.decision, scored_at=excluded.scored_at,
                    decided_at=COALESCE(excluded.decided_at, face_attempts.decided_at)
                """,
                (
                    fn, parsed.event_id, parsed.frame_ts, parsed.name,
                    parsed.recog_score, sharpness, area, quality, decision,
                    _now(), decided_at,
                ),
            )
            if fn in existing:
                summary["rescored"] += 1
            else:
                summary["new"] += 1
        conn.commit()
    finally:
        conn.close()
        if promote_client is not None:
            promote_client.close()
    return summary


def histogram(settings: Settings, bins: int = 10) -> dict[str, Any]:
    """Quality-score distribution + decision/recognition counts.

    This is the B2 gate signal: it tells us how bad the auto-saved pool actually
    is before deciding whether the heavier main-stream re-capture is worth it.
    """
    conn = open_sidecar(settings.sidecar.db_path)
    try:
        rows = conn.execute(
            "SELECT quality_score, sharpness, area_px, recognized_name, decision "
            "FROM face_attempts"
        ).fetchall()
    finally:
        conn.close()

    qs = [r["quality_score"] for r in rows if r["quality_score"] is not None]
    counts = [0] * bins
    for q in qs:
        idx = min(bins - 1, int(q * bins))
        counts[idx] += 1
    by_decision: dict[str, int] = {}
    by_recog: dict[str, int] = {"recognized": 0, "unknown": 0}
    for r in rows:
        by_decision[r["decision"]] = by_decision.get(r["decision"], 0) + 1
        key = "unknown" if (r["recognized_name"] or "unknown") == "unknown" else "recognized"
        by_recog[key] += 1

    qs_sorted = sorted(qs)
    return {
        "total": len(rows),
        "bins": bins,
        "histogram": counts,
        "by_decision": by_decision,
        "by_recognition": by_recog,
        "quality_min": qs_sorted[0] if qs_sorted else None,
        "quality_median": qs_sorted[len(qs_sorted) // 2] if qs_sorted else None,
        "quality_max": qs_sorted[-1] if qs_sorted else None,
    }
