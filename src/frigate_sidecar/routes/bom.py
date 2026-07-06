"""BOM builder: assemble a KiCad-style Master BOM one part at a time.

This page is deliberately unrelated to Frigate analysis — it's a hardware
engineering tool. It ports the user's Excel "Master BOM Template" workflow into
the sidecar: seed lines (manually or by importing a KiCad grouped-BOM CSV), then
enrich each line one part at a time, and hand the result to backend automation as
JSON, a 94-column Master-BOM CSV, or a kicad-parts ``approved_parts.csv``.

State lives entirely in the sidecar DB (``bom_projects`` + ``bom_items``). The
field model, computed columns, and validation vocabularies live in
``frigate_sidecar.bom_schema``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from frigate_sidecar import bom_schema
from frigate_sidecar.bom_schema import (
    APPROVED_PARTS_COLUMNS,
    DB_CONTENT_COLUMNS,
    EXTRA_KEYS,
    KICAD_IMPORT_MAP,
    MASTER_BOM_COLUMNS,
    VALIDATION_LISTS,
    approved_parts_row,
    category_from_designator,
    item_public,
    master_row,
)
from frigate_sidecar.config import Settings
from frigate_sidecar.db import open_sidecar

router = APIRouter(tags=["bom"])

_PROJECT_COLUMNS = (
    "project_name", "board_name", "pcb_revision", "bom_revision", "build_quantity",
    "attrition_pct", "currency", "assembly_vendor", "assembly_method_default",
    "owner", "source_cad_tool", "notes",
)


# --- helpers -----------------------------------------------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def open_conn(request: Request) -> sqlite3.Connection:
    return open_sidecar(_settings(request).sidecar.db_path)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "bom"


def _unique_slug(conn: sqlite3.Connection, base: str) -> str:
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM bom_projects WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _project_row(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM bom_projects WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"BOM project not found: {slug}")
    return row


def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


def _items_public(conn: sqlite3.Connection, project: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM bom_items WHERE project_id = ? ORDER BY item_no, id",
        (project["id"],),
    ).fetchall()
    return [item_public(project, r) for r in rows]


def _rollup(project: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """Cost / completeness rollup — the workbook's Cost_Rollup sheet, in Python."""
    installed = [i for i in items if str(i.get("populate", "")).upper() in ("YES", "OPT", "VAR")]

    def _missing(val: Any) -> bool:
        return not val or str(val).strip().upper() in ("", "TBD")

    cost_per_assembly = sum(i["cost_per_assembly"] or 0 for i in installed)
    extended_buy_cost = sum(i["extended_line_cost"] or 0 for i in installed)

    by_cat: dict[str, dict[str, float]] = {}
    for i in installed:
        cat = i.get("part_category") or "Uncategorized"
        b = by_cat.setdefault(cat, {"lines": 0, "qty_per_assembly": 0.0, "cost_per_assembly": 0.0})
        b["lines"] += 1
        b["qty_per_assembly"] += bom_schema._num(i.get("qty_per_assembly"))
        b["cost_per_assembly"] += i["cost_per_assembly"] or 0

    return {
        "populated_lines": len(installed),
        "dnp_lines": len(items) - len(installed),
        "total_lines": len(items),
        "missing_mpn_lines": sum(1 for i in installed if _missing(i.get("mpn"))),
        "missing_dpn_lines": sum(1 for i in installed if _missing(i.get("preferred_dpn"))),
        "needs_review_lines": sum(
            1 for i in items
            if str(i.get("review_status", "")).strip() in ("Needs Review", "Unchecked", "")
        ),
        "high_risk_lines": sum(1 for i in items if i.get("risk_level") in ("High", "Critical")),
        "estimated_cost_per_assembly": round(cost_per_assembly, 4),
        "estimated_extended_buy_cost": round(extended_buy_cost, 4),
        "currency": project.get("currency") or "USD",
        "by_category": by_cat,
    }


# --- payloads ----------------------------------------------------------------


class BomProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str = Field(..., min_length=1)
    board_name: str | None = None
    pcb_revision: str | None = None
    bom_revision: str | None = None
    build_quantity: int = Field(1, ge=1)
    attrition_pct: float = Field(0.05, ge=0)
    currency: str = "USD"
    assembly_vendor: str | None = None
    assembly_method_default: str = "SMT"
    owner: str | None = None
    source_cad_tool: str = "KiCad"
    notes: str | None = None


class BomItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_no: int | None = None
    designator: str | None = None
    populate: str = "YES"
    variant: str = "Base"
    qty_per_assembly: float = Field(1, ge=0)
    symbol: str | None = None
    footprint: str | None = None
    part_category: str | None = None
    value: str | None = None
    description: str | None = None
    package_size: str | None = None
    manufacturer: str | None = None
    mpn: str = "TBD"
    datasheet_url: str | None = None
    preferred_distributor: str | None = None
    preferred_dpn: str = "TBD"
    distributor_url: str | None = None
    do_not_substitute: str = "N"
    lifecycle_status: str = "TBD"
    moq: int | None = Field(None, ge=0)
    order_multiple: int | None = Field(None, ge=0)
    unit_cost: float | None = Field(None, ge=0)
    risk_level: str = "Unknown"
    review_status: str = "Needs Review"
    comment: str | None = None
    source: str = "Manual"
    # Advanced Master-BOM fields (stored together as JSON). Unknown keys dropped.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("populate")
    @classmethod
    def _valid_populate(cls, v: str) -> str:
        if v and v not in VALIDATION_LISTS["populate"]:
            raise ValueError(f"populate must be one of {VALIDATION_LISTS['populate']}")
        return v


class KicadImportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str = Field(..., min_length=1)


# --- item persistence --------------------------------------------------------


def _item_write_values(payload: BomItemIn, item_no: int) -> dict[str, Any]:
    data = payload.model_dump()
    extra = {k: v for k, v in (data.pop("extra") or {}).items() if k in EXTRA_KEYS}
    values: dict[str, Any] = {c: data.get(c) for c in DB_CONTENT_COLUMNS}
    values["item_no"] = item_no
    values["extra_fields"] = json.dumps(extra) if extra else None
    return values


def _next_item_no(conn: sqlite3.Connection, project_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(item_no), 0) AS m FROM bom_items WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    return int(row["m"]) + 1


def _insert_item(conn: sqlite3.Connection, project_id: int, values: dict[str, Any]) -> int:
    now = _now()
    cols = ["project_id", *values.keys(), "created_at", "updated_at"]
    placeholders = ", ".join("?" for _ in cols)
    params = [project_id, *values.values(), now, now]
    cur = conn.execute(
        f"INSERT INTO bom_items ({', '.join(cols)}) VALUES ({placeholders})", params
    )
    # lastrowid is always set after a successful INSERT; the `or 0` only satisfies typing.
    return int(cur.lastrowid or 0)


# --- pages -------------------------------------------------------------------


@router.get("/bom", response_class=HTMLResponse)
def bom_index(request: Request) -> Any:
    conn = open_conn(request)
    try:
        rows = conn.execute(
            """
            SELECT p.*, (
                SELECT COUNT(*) FROM bom_items i WHERE i.project_id = p.id
            ) AS item_count
            FROM bom_projects p ORDER BY p.updated_at DESC
            """
        ).fetchall()
        projects = [_project_dict(r) | {"item_count": r["item_count"]} for r in rows]
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request, "bom.html", {"projects": projects}
    )


# --- project API -------------------------------------------------------------
# NOTE: static single-segment routes (/bom/projects) MUST be declared before the
# /bom/{slug} catch-all, or the catch-all captures "projects" as a slug. Two-
# segment routes (/bom/{slug}/...) never collide, so their order is free.


@router.get("/bom/projects")
def bom_projects_list(request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        rows = conn.execute("SELECT * FROM bom_projects ORDER BY updated_at DESC").fetchall()
        return JSONResponse({"projects": [_project_dict(r) for r in rows]})
    finally:
        conn.close()


@router.post("/bom/projects")
def bom_project_create(payload: BomProjectIn, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        slug = _unique_slug(conn, _slugify(payload.project_name))
        now = _now()
        data = payload.model_dump()
        cols = ["slug", *_PROJECT_COLUMNS, "created_at", "updated_at"]
        params = [slug, *[data[c] for c in _PROJECT_COLUMNS], now, now]
        conn.execute(
            f"INSERT INTO bom_projects ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            params,
        )
        conn.commit()
        row = _project_row(conn, slug)
        return JSONResponse({"ok": True, "project": _project_dict(row)}, status_code=201)
    finally:
        conn.close()


@router.get("/bom/{slug}", response_class=HTMLResponse)
def bom_project_view(slug: str, request: Request) -> Any:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
    finally:
        conn.close()
    return request.app.state.templates.TemplateResponse(
        request,
        "bom_project.html",
        {
            "project": project,
            "core_fields": bom_schema.CORE_FIELDS,
            "advanced_fields": bom_schema.ADVANCED_FIELDS,
            "computed_fields": bom_schema.COMPUTED_FIELDS,
            "validation_lists": VALIDATION_LISTS,
        },
    )


@router.get("/bom/{slug}/config")
def bom_project_config(slug: str, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        return JSONResponse({"project": _project_dict(_project_row(conn, slug))})
    finally:
        conn.close()


@router.put("/bom/{slug}/config")
def bom_project_update(slug: str, payload: BomProjectIn, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        _project_row(conn, slug)  # 404 if missing
        data = payload.model_dump()
        assignments = ", ".join(f"{c} = ?" for c in _PROJECT_COLUMNS)
        params = [*[data[c] for c in _PROJECT_COLUMNS], _now(), slug]
        conn.execute(
            f"UPDATE bom_projects SET {assignments}, updated_at = ? WHERE slug = ?", params
        )
        conn.commit()
        return JSONResponse({"ok": True, "project": _project_dict(_project_row(conn, slug))})
    finally:
        conn.close()


@router.delete("/bom/{slug}")
def bom_project_delete(slug: str, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        _project_row(conn, slug)
        conn.execute("DELETE FROM bom_projects WHERE slug = ?", (slug,))
        conn.commit()
        return JSONResponse({"ok": True})
    finally:
        conn.close()


# --- item API ----------------------------------------------------------------


@router.get("/bom/{slug}/items")
def bom_items_list(slug: str, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        items = _items_public(conn, project)
        return JSONResponse(
            {"project": project, "items": items, "rollup": _rollup(project, items)}
        )
    finally:
        conn.close()


@router.post("/bom/{slug}/items")
def bom_item_create(slug: str, payload: BomItemIn, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        item_no = payload.item_no or _next_item_no(conn, project["id"])
        values = _item_write_values(payload, item_no)
        item_id = _insert_item(conn, project["id"], values)
        conn.commit()
        row = conn.execute("SELECT * FROM bom_items WHERE id = ?", (item_id,)).fetchone()
        return JSONResponse({"ok": True, "item": item_public(project, row)}, status_code=201)
    finally:
        conn.close()


@router.put("/bom/{slug}/items/{item_id}")
def bom_item_update(slug: str, item_id: int, payload: BomItemIn, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        existing = conn.execute(
            "SELECT * FROM bom_items WHERE id = ? AND project_id = ?", (item_id, project["id"])
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail=f"BOM item not found: {item_id}")
        item_no = payload.item_no or existing["item_no"]
        values = _item_write_values(payload, item_no)
        assignments = ", ".join(f"{c} = ?" for c in values)
        params = [*values.values(), _now(), item_id]
        conn.execute(
            f"UPDATE bom_items SET {assignments}, updated_at = ? WHERE id = ?", params
        )
        conn.commit()
        row = conn.execute("SELECT * FROM bom_items WHERE id = ?", (item_id,)).fetchone()
        return JSONResponse({"ok": True, "item": item_public(project, row)})
    finally:
        conn.close()


@router.delete("/bom/{slug}/items/{item_id}")
def bom_item_delete(slug: str, item_id: int, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        cur = conn.execute(
            "DELETE FROM bom_items WHERE id = ? AND project_id = ?", (item_id, project["id"])
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"BOM item not found: {item_id}")
        return JSONResponse({"ok": True})
    finally:
        conn.close()


# --- KiCad CSV import --------------------------------------------------------


@router.post("/bom/{slug}/import/kicad")
def bom_import_kicad(slug: str, payload: KicadImportIn, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        reader = csv.DictReader(io.StringIO(payload.csv_text))
        if reader.fieldnames is None:
            raise HTTPException(status_code=400, detail="CSV has no header row")
        # Case/space-insensitive match of KiCad columns to our import map.
        norm = {re.sub(r"\s+", " ", (h or "").strip()).lower(): h for h in reader.fieldnames}
        colmap = {
            norm[k.lower()]: field for k, field in KICAD_IMPORT_MAP.items() if k.lower() in norm
        }
        if not colmap:
            raise HTTPException(
                status_code=400,
                detail="No recognized KiCad columns (expected Id, Designator, Footprint, "
                "Quantity, Designation, Supplier and ref)",
            )

        next_no = _next_item_no(conn, project["id"])
        added = 0
        for raw in reader:
            mapped: dict[str, Any] = {}
            for src_col, field in colmap.items():
                val = (raw.get(src_col) or "").strip()
                if val:
                    mapped[field] = val
            if not any(mapped.get(k) for k in ("designator", "value", "footprint")):
                continue  # skip blank rows
            mapped.setdefault("qty_per_assembly", "1")
            mapped["part_category"] = category_from_designator(mapped.get("designator"))
            mapped["source"] = "Imported from KiCad CSV"
            try:
                payload_item = BomItemIn(**mapped)
            except ValueError:
                # Fall back: drop the offending fields we can't validate, keep the row.
                mapped.pop("populate", None)
                payload_item = BomItemIn(**mapped)
            values = _item_write_values(payload_item, item_no=next_no)
            _insert_item(conn, project["id"], values)
            next_no += 1
            added += 1
        conn.commit()
        items = _items_public(conn, project)
        return JSONResponse({"ok": True, "added": added, "total": len(items)})
    finally:
        conn.close()


# --- exports / handoff -------------------------------------------------------


@router.get("/bom/{slug}/export.json")
def bom_export_json(slug: str, request: Request) -> JSONResponse:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        items = _items_public(conn, project)
        return JSONResponse(
            {
                "project": project,
                "columns": MASTER_BOM_COLUMNS,
                "items": items,
                "rollup": _rollup(project, items),
            }
        )
    finally:
        conn.close()


def _csv_response(headers: list[str], rows: list[dict[str, Any]], filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({h: bom_schema.tidy_number(row.get(h, "")) for h in headers})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/bom/{slug}/export.csv")
def bom_export_master_csv(slug: str, request: Request) -> Response:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        rows = conn.execute(
            "SELECT * FROM bom_items WHERE project_id = ? ORDER BY item_no, id", (project["id"],)
        ).fetchall()
        master = [master_row(project, bom_schema.item_flat(r)) for r in rows]
    finally:
        conn.close()
    return _csv_response(MASTER_BOM_COLUMNS, master, f"{slug}_master_bom.csv")


@router.get("/bom/{slug}/approved_parts.csv")
def bom_export_approved_parts(slug: str, request: Request) -> Response:
    conn = open_conn(request)
    try:
        project = _project_dict(_project_row(conn, slug))
        rows = conn.execute(
            "SELECT * FROM bom_items WHERE project_id = ? ORDER BY item_no, id", (project["id"],)
        ).fetchall()
        # Only installed lines that carry a real MPN belong in approved_parts.
        approved: list[dict[str, Any]] = []
        for r in rows:
            flat = bom_schema.item_flat(r)
            if str(flat.get("populate", "")).upper() not in ("YES", "OPT", "VAR"):
                continue
            if str(flat.get("mpn") or "").strip().upper() in ("", "TBD"):
                continue
            approved.append(approved_parts_row(flat))
    finally:
        conn.close()
    return _csv_response(APPROVED_PARTS_COLUMNS, approved, f"{slug}_approved_parts.csv")
