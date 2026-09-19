"""`EncounterService`: the live MQTT hook, the reconciler ("belt"), and
`/healthz` status (docs/encounters.md "service.py").
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

from frigate_sidecar import db
from frigate_sidecar.analysis.clock_offset import event_clock_offset_s
from frigate_sidecar.config import Settings
from frigate_sidecar.encounters import store
from frigate_sidecar.encounters.adjacency import Adjacency
from frigate_sidecar.encounters.linker import Atom, LinkerConfig, apply, decide
from frigate_sidecar.push.models import ReviewEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileStats:
    rows: int
    new: int
    updated: int
    sealed: int


def _linker_config(settings: Settings) -> LinkerConfig:
    enc = settings.encounters
    return LinkerConfig(
        gap_s=dict(enc.gap_s),
        max_duration_s=enc.max_duration_s,
        recent_cameras=enc.recent_cameras,
        min_copresence_s=enc.min_copresence_s,
    )


def _review_data(raw: object) -> dict[str, object]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(x) for x in value)


class EncounterService:
    """Owns encounter linking: `observe_review` is the live per-message hook
    (wired as `PushEngine`'s `on_review`), `reconcile` is the periodic
    backfill/repair sweep run via `asyncio.to_thread`.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        adjacency: Adjacency,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.adjacency = adjacency
        self._now = now
        self._cfg = _linker_config(settings)
        self._last_reconcile: ReconcileStats | None = None
        self._last_reconcile_at: float | None = None
        self._last_error: str | None = None

    def _conn(self) -> sqlite3.Connection:
        return db.open_sidecar(self.settings.sidecar.db_path)

    def _pin_split(
        self, conn: sqlite3.Connection, atom_id: str
    ) -> tuple[str | None, frozenset[str]]:
        decisions = store.decisions_for(conn, atom_id)
        pinned_to = next((d["encounter_id"] for d in decisions if d["action"] == "pin"), None)
        split_from = frozenset(d["encounter_id"] for d in decisions if d["action"] == "split")
        return pinned_to, split_from

    def observe_review(self, ev: ReviewEvent) -> None:
        """LIVE hook off `PushEngine.handle_event` -- never raises."""
        try:
            now = self._now()
            end_time = now if ev.msg_type == "end" else None
            atom = Atom(
                atom_id=ev.review_id,
                camera=ev.camera,
                start_time=ev.start_time,
                end_time=end_time,
                labels=ev.labels,
                zones=ev.zones,
                event_ids=ev.track_ids,
                sub_labels=ev.sub_labels,
                severity=ev.severity,
            )
            conn = self._conn()
            try:
                pinned_to, split_from = self._pin_split(conn, atom.atom_id)
                open_encounters = store.load_open(conn)
                decision = decide(
                    atom,
                    open_encounters,
                    self.adjacency,
                    self._cfg,
                    pinned_to=pinned_to,
                    split_from=split_from,
                )
                store.upsert_atom(conn, atom, decision, now)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 -- an encounters failure must never affect push
            logger.exception("encounters: observe_review failed for review %s", ev.review_id)

    def _atom_from_review_row(self, row: sqlite3.Row) -> Atom:
        camera = str(row["camera"])
        offset_s = event_clock_offset_s(self.settings, camera)
        data = _review_data(row["data"])
        start_time = float(row["start_time"]) + offset_s
        end_time = float(row["end_time"]) + offset_s if row["end_time"] is not None else None
        return Atom(
            atom_id=str(row["id"]),
            camera=camera,
            start_time=start_time,
            end_time=end_time,
            labels=_strings(data.get("objects")),
            zones=_strings(data.get("zones")),
            event_ids=_strings(data.get("detections")),
            sub_labels=_strings(data.get("sub_labels")),
            severity=str(row["severity"]),
        )

    def reconcile(self) -> ReconcileStats:
        """BACKFILL/repair sweep over `reviewsegment` -- the "belt" catching
        anything the MQTT path missed or saw only partially. Sync; run via
        `asyncio.to_thread` from the server's loop."""
        now = self._now()
        frigate_conn = db.open_frigate_ro(self.settings.frigate.db_path)
        sidecar_conn = self._conn()
        try:
            watermark = store.get_watermark(sidecar_conn)
            if watermark is None:
                since = now - self.settings.encounters.backfill_lookback_s
            else:
                since = watermark - self._cfg.max_duration_s

            try:
                rows = frigate_conn.execute(
                    "SELECT id, camera, start_time, end_time, severity, data "
                    "FROM reviewsegment WHERE start_time >= ? ORDER BY start_time",
                    (since,),
                ).fetchall()
            except sqlite3.Error:
                rows = []

            atoms = sorted(
                (self._atom_from_review_row(row) for row in rows), key=lambda a: a.start_time
            )

            open_encounters = store.load_open(sidecar_conn)
            by_id = {e.encounter_id: e for e in open_encounters}
            new_count = 0
            updated_count = 0
            for atom in atoms:
                existing = sidecar_conn.execute(
                    "SELECT 1 FROM encounter_members WHERE atom_id = ?", (atom.atom_id,)
                ).fetchone()
                pinned_to, split_from = self._pin_split(sidecar_conn, atom.atom_id)
                decision = decide(
                    atom,
                    list(by_id.values()),
                    self.adjacency,
                    self._cfg,
                    pinned_to=pinned_to,
                    split_from=split_from,
                )
                encounter_id = store.upsert_atom(sidecar_conn, atom, decision, now)
                if encounter_id in by_id:
                    apply(by_id[encounter_id], atom)
                else:
                    refreshed = store.load_one(sidecar_conn, encounter_id)
                    if refreshed is not None:
                        by_id[encounter_id] = refreshed
                if existing is None:
                    new_count += 1
                else:
                    updated_count += 1

            sealed = store.seal_stale(sidecar_conn, now, self._cfg)
            if atoms:
                store.set_watermark(sidecar_conn, max(a.start_time for a in atoms))

            stats = ReconcileStats(
                rows=len(rows), new=new_count, updated=updated_count, sealed=sealed
            )
            self._last_reconcile = stats
            self._last_reconcile_at = now
            self._last_error = None
            return stats
        except Exception as exc:
            self._last_error = str(exc)
            raise
        finally:
            frigate_conn.close()
            sidecar_conn.close()

    def status(self) -> dict[str, object]:
        """Status summary for `/healthz`."""
        state = "error" if self._last_error is not None else "ok"
        out: dict[str, object] = {"state": state}
        if self._last_reconcile is not None:
            out["last_reconcile"] = {
                "rows": self._last_reconcile.rows,
                "new": self._last_reconcile.new,
                "updated": self._last_reconcile.updated,
                "sealed": self._last_reconcile.sealed,
                "age_s": round(self._now() - (self._last_reconcile_at or self._now()), 1),
            }
        if self._last_error is not None:
            out["error"] = self._last_error
        return out
