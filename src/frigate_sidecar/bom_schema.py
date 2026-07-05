"""Single source of truth for the BOM builder's field model.

This encodes the three "reference" sheets of the user's Master BOM workbook so the
entry form, the CSV/JSON exports, and the computed rollup all agree on one schema:

* ``Field_Definitions``  -> :data:`FIELDS` (the 94 Master-BOM columns, in order).
* ``Validation_Lists``   -> :data:`VALIDATION_LISTS` (dropdown vocabularies).
* ``Import_Mapping``     -> :data:`KICAD_IMPORT_MAP` + :func:`category_from_designator`.

The 94 columns split three ways:

* **core / db** columns are stored as real, queryable columns on ``bom_items``
  (see ``SIDECAR_SCHEMA`` in ``db.py``). ``core`` ones are shown up front in the
  form; the rest sit in the collapsible "advanced" section.
* **extra** columns are stored together as a JSON blob (``bom_items.extra_fields``)
  so the full 94-field superset round-trips without a 94-column table.
* **computed** columns are never stored; they are derived on read/export, exactly
  as the workbook treats them as formulas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from math import ceil
from typing import Any

# --- Controlled vocabularies (workbook "Validation_Lists" sheet) --------------

VALIDATION_LISTS: dict[str, list[str]] = {
    "populate": ["YES", "DNP", "OPT", "VAR", "TBD"],
    "variant": ["Base", "Prototype", "Production", "Debug", "Alternate"],
    "pcb_side": ["Top", "Bottom", "Both", "N/A", "TBD"],
    "part_category": [
        "Capacitor", "Resistor", "IC", "Connector", "Diode", "Inductor",
        "Crystal / Oscillator", "Transistor", "Fuse", "Switch", "Relay", "LED",
        "Test Point", "Module", "Mechanical", "Hardware", "Cable", "Label", "Other",
    ],
    "distributor": [
        "DigiKey", "Mouser", "LCSC", "JLCPCB", "Arrow", "Avnet", "Newark", "TME",
        "Amazon", "AliExpress", "Manufacturer Direct", "Other", "TBD",
    ],
    "lifecycle_status": ["Active", "NRND", "EOL", "Obsolete", "Unknown", "TBD"],
    "packaging": [
        "Cut Tape", "Reel", "Mini Reel", "Tray", "Tube", "Bag", "Bulk", "Kit", "TBD",
    ],
    "assembly_method": [
        "SMT", "THT", "Hand Install", "Mechanical", "Cable", "Not Assembled", "TBD",
    ],
    "placement_type": ["Machine", "Hand", "Do Not Place", "N/A", "TBD"],
    "msl": ["1", "2", "2A", "3", "4", "5", "5A", "6", "Unknown", "N/A"],
    "test_coverage": [
        "Visual", "ICT", "Flying Probe", "Functional", "Boundary Scan",
        "Programming Only", "Not Tested", "TBD",
    ],
    "risk_level": ["Low", "Medium", "High", "Critical", "Unknown"],
    "currency": ["USD", "CNY", "EUR", "GBP", "JPY", "CAD"],
    "review_status": ["Unchecked", "Needs Review", "Approved", "Rejected", "Blocked"],
    "yes_no_unknown": ["Y", "N", "Unknown", "N/A"],
}


# --- Field registry (workbook "BOM_Master" header row / "Field_Definitions") ---


@dataclass(frozen=True)
class Field:
    """One Master-BOM column.

    ``kind`` is one of:
      * ``core``  -> real DB column, shown up front in the form.
      * ``db``    -> real DB column, shown in the advanced section.
      * ``extra`` -> stored in the ``extra_fields`` JSON blob (advanced section).
      * ``calc``  -> computed on read/export, never stored.
    """

    header: str
    key: str
    kind: str
    input: str = "text"
    options: str | None = None

    @property
    def db_col(self) -> str | None:
        return self.key if self.kind in ("core", "db") else None

    @property
    def computed(self) -> bool:
        return self.kind == "calc"


# (header, key, kind, input, options) — order matches the workbook's BOM_Master
# header row verbatim so the exported CSV is a drop-in for the spreadsheet.
_SPEC: list[tuple[str, str, str, str, str | None]] = [
    ("Item #", "item_no", "db", "auto", None),
    ("Designator / RefDes", "designator", "core", "text", None),
    ("RefDes Count", "refdes_count", "calc", "", None),
    ("Populate", "populate", "core", "select", "populate"),
    ("Variant", "variant", "core", "select", "variant"),
    ("Quantity Per Assembly", "qty_per_assembly", "core", "number", None),
    ("Quantity Check", "quantity_check", "calc", "", None),
    ("Symbol", "symbol", "db", "text", None),
    ("Footprint", "footprint", "core", "text", None),
    ("PCB Side", "pcb_side", "extra", "select", "pcb_side"),
    ("Circuit Block", "circuit_block", "extra", "text", None),
    ("Schematic Sheet", "schematic_sheet", "extra", "text", None),
    ("Part Category", "part_category", "core", "select", "part_category"),
    ("Value", "value", "core", "text", None),
    ("Description", "description", "core", "text", None),
    ("Tolerance", "tolerance", "extra", "text", None),
    ("Voltage Rating", "voltage_rating", "extra", "text", None),
    ("Power Rating", "power_rating", "extra", "text", None),
    ("Current Rating", "current_rating", "extra", "text", None),
    ("Dielectric / Material", "dielectric_material", "extra", "text", None),
    ("Package Size", "package_size", "db", "text", None),
    ("Operating Temp Range", "operating_temp_range", "extra", "text", None),
    ("Height", "height", "extra", "text", None),
    ("Manufacturer", "manufacturer", "core", "text", None),
    ("MPN", "mpn", "core", "text", None),
    ("Manufacturer Part URL", "manufacturer_part_url", "extra", "text", None),
    ("Datasheet URL", "datasheet_url", "core", "text", None),
    ("Preferred Distributor", "preferred_distributor", "core", "select", "distributor"),
    ("Preferred DPN", "preferred_dpn", "core", "text", None),
    ("Distributor URL", "distributor_url", "db", "text", None),
    ("Distributor 2", "distributor_2", "extra", "select", "distributor"),
    ("DPN 2", "dpn_2", "extra", "text", None),
    ("Distributor 2 URL", "distributor_2_url", "extra", "text", None),
    ("Approved Alternate MPNs", "approved_alternate_mpns", "extra", "text", None),
    ("Do Not Substitute", "do_not_substitute", "db", "select", "yes_no_unknown"),
    ("Substitution Notes", "substitution_notes", "extra", "text", None),
    ("Lifecycle Status", "lifecycle_status", "core", "select", "lifecycle_status"),
    ("Stock", "stock", "extra", "text", None),
    ("Lead Time", "lead_time", "extra", "text", None),
    ("MOQ", "moq", "db", "number", None),
    ("Order Multiple", "order_multiple", "db", "number", None),
    ("Packaging", "packaging", "extra", "select", "packaging"),
    ("Last Verified Date", "last_verified_date", "extra", "text", None),
    ("Build Quantity", "build_quantity", "calc", "", None),
    ("Total Required", "total_required", "calc", "", None),
    ("Attrition %", "attrition_pct", "calc", "", None),
    ("Attrition Qty", "attrition_qty", "calc", "", None),
    ("Buy Quantity", "buy_quantity", "calc", "", None),
    ("Unit Cost @ 1", "unit_cost_1", "extra", "number", None),
    ("Unit Cost @ 10", "unit_cost_10", "extra", "number", None),
    ("Unit Cost @ 100", "unit_cost_100", "extra", "number", None),
    ("Unit Cost @ 1k", "unit_cost_1k", "extra", "number", None),
    ("Selected Unit Cost", "unit_cost", "core", "number", None),
    ("Cost Per Assembly", "cost_per_assembly", "calc", "", None),
    ("Extended Line Cost", "extended_line_cost", "calc", "", None),
    ("Currency", "currency", "calc", "", None),
    ("Quote Date", "quote_date", "extra", "text", None),
    ("Assembly Method", "assembly_method", "extra", "select", "assembly_method"),
    ("Placement Type", "placement_type", "extra", "select", "placement_type"),
    ("Polarized", "polarized", "extra", "select", "yes_no_unknown"),
    ("Orientation Critical", "orientation_critical", "extra", "select", "yes_no_unknown"),
    ("Polarity / Orientation Notes", "polarity_orientation_notes", "extra", "text", None),
    ("MSL", "msl", "extra", "select", "msl"),
    ("Reflow Compatible", "reflow_compatible", "extra", "select", "yes_no_unknown"),
    ("Wash Compatible", "wash_compatible", "extra", "select", "yes_no_unknown"),
    ("Requires Programming", "requires_programming", "extra", "select", "yes_no_unknown"),
    ("Requires Calibration", "requires_calibration", "extra", "select", "yes_no_unknown"),
    ("Assembly Notes", "assembly_notes", "extra", "text", None),
    ("CM Notes", "cm_notes", "extra", "text", None),
    ("RoHS", "rohs", "extra", "select", "yes_no_unknown"),
    ("REACH", "reach", "extra", "select", "yes_no_unknown"),
    ("Country of Origin", "country_of_origin", "extra", "text", None),
    ("HTS Code", "hts_code", "extra", "text", None),
    ("ECCN", "eccn", "extra", "text", None),
    ("Critical Part", "critical_part", "extra", "select", "yes_no_unknown"),
    ("Safety Critical", "safety_critical", "extra", "select", "yes_no_unknown"),
    ("Test Coverage", "test_coverage", "extra", "select", "test_coverage"),
    ("Inspection Requirement", "inspection_requirement", "extra", "text", None),
    ("Known Risk", "known_risk", "extra", "text", None),
    ("Risk Level", "risk_level", "core", "select", "risk_level"),
    ("Owner", "owner", "extra", "text", None),
    ("Review Status", "review_status", "core", "select", "review_status"),
    ("Verified By", "verified_by", "extra", "text", None),
    ("Verified Date", "verified_date", "extra", "text", None),
    ("Issue / ECO Link", "issue_eco_link", "extra", "text", None),
    ("Introduced In Rev", "introduced_in_rev", "extra", "text", None),
    ("Removed In Rev", "removed_in_rev", "extra", "text", None),
    ("BOM Revision", "bom_revision", "extra", "text", None),
    ("PCB Revision", "pcb_revision", "extra", "text", None),
    ("Schematic Revision", "schematic_revision", "extra", "text", None),
    ("ECO / Change Reason", "eco_change_reason", "extra", "text", None),
    ("Change Notes", "change_notes", "extra", "text", None),
    ("Source", "source", "db", "text", None),
    ("Comment", "comment", "core", "text", None),
]

FIELDS: list[Field] = [Field(h, k, kind, inp, opt) for (h, k, kind, inp, opt) in _SPEC]

BY_HEADER: dict[str, Field] = {f.header: f for f in FIELDS}
BY_KEY: dict[str, Field] = {f.key: f for f in FIELDS}

#: Ordered 94 headers — the Master-BOM CSV export column order.
MASTER_BOM_COLUMNS: list[str] = [f.header for f in FIELDS]

#: Content columns stored as real DB columns on ``bom_items``.
DB_CONTENT_COLUMNS: list[str] = [f.key for f in FIELDS if f.db_col]

#: Shown up front in the add/edit form.
CORE_FIELDS: list[Field] = [f for f in FIELDS if f.kind == "core"]
#: Shown in the collapsible "advanced" section (real-but-not-core + extra).
ADVANCED_FIELDS: list[Field] = [f for f in FIELDS if f.kind in ("db", "extra")]
#: Derived, read-only.
COMPUTED_FIELDS: list[Field] = [f for f in FIELDS if f.computed]

#: Keys stored inside the ``extra_fields`` JSON blob.
EXTRA_KEYS: list[str] = [f.key for f in FIELDS if f.kind == "extra"]


# --- kicad-parts approved_parts.csv contract ---------------------------------

#: The 12 canonical columns kicad-parts' ``approved_parts.csv`` carries (kp sync).
APPROVED_PARTS_COLUMNS: list[str] = [
    "mpn", "manufacturer", "datasheet", "alt_mpn_1", "alt_mpn_2",
    "lifecycle_status", "preferred_distributor", "min_qty_price",
    "package", "value", "function", "notes",
]


# --- KiCad CSV import (workbook "Import_Mapping" sheet) -----------------------

#: KiCad grouped-BOM CSV column -> ``bom_items`` field key.
KICAD_IMPORT_MAP: dict[str, str] = {
    "Id": "item_no",
    "Designator": "designator",
    "Footprint": "footprint",
    "Quantity": "qty_per_assembly",
    "Designation": "value",
    "Supplier and ref": "comment",
}

# Longest-prefix-first so "LED"/"SW"/"TP" win over "L"/"S"/"T".
_CATEGORY_BY_PREFIX: list[tuple[str, str]] = [
    ("LED", "LED"),
    ("SW", "Switch"),
    ("TP", "Test Point"),
    ("FB", "Inductor"),
    ("MH", "Hardware"),
    ("XT", "Crystal / Oscillator"),
    ("C", "Capacitor"),
    ("R", "Resistor"),
    ("U", "IC"),
    ("J", "Connector"),
    ("P", "Connector"),
    ("D", "Diode"),
    ("L", "Inductor"),
    ("Y", "Crystal / Oscillator"),
    ("X", "Crystal / Oscillator"),
    ("Q", "Transistor"),
    ("F", "Fuse"),
    ("S", "Switch"),
    ("K", "Relay"),
    ("H", "Hardware"),
    ("M", "Module"),
]


def category_from_designator(designator: str | None) -> str:
    """Infer a Part Category from a reference designator's letter prefix.

    Mirrors the workbook's Import_Mapping rule ("category inferred from first
    designator prefix"). Returns "" when nothing matches.
    """
    first = (designator or "").split(",")[0].strip().upper()
    letters = "".join(ch for ch in first if ch.isalpha())
    for prefix, category in _CATEGORY_BY_PREFIX:
        if letters.startswith(prefix):
            return category
    return ""


# --- helpers -----------------------------------------------------------------


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def tidy_number(value: Any) -> Any:
    """Render whole floats as ints (1.0 -> 1) for clean CSV/JSON; pass others."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def compute_fields(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Derive the 10 formula columns for one line (mirrors the workbook formulas)."""
    designator = item.get("designator") or ""
    refdes_count = len([d for d in designator.split(",") if d.strip()])
    qpa = _num(item.get("qty_per_assembly"), 0.0)
    build_qty = _num(project.get("build_quantity"), 1.0)
    total_required = qpa * build_qty
    attrition_pct = _num(project.get("attrition_pct"), 0.0)
    attrition_qty = ceil(total_required * attrition_pct) if total_required > 0 else 0

    buy = total_required + attrition_qty
    moq = _num(item.get("moq"), 0.0)
    order_mult = _num(item.get("order_multiple"), 0.0)
    if moq > 0 and buy < moq:
        buy = moq
    if order_mult > 0:
        buy = ceil(buy / order_mult) * order_mult

    unit_cost = item.get("unit_cost")
    has_cost = unit_cost not in (None, "")
    cost = _num(unit_cost, 0.0)
    cost_per_assembly = cost * qpa if has_cost else None
    extended_line_cost = cost * buy if has_cost else None

    # OK unless a designator count is present and disagrees with the qty.
    quantity_check = "CHECK" if refdes_count and refdes_count != qpa else "OK"

    return {
        "refdes_count": refdes_count,
        "quantity_check": quantity_check,
        "build_quantity": tidy_number(build_qty),
        "total_required": tidy_number(total_required),
        "attrition_pct": attrition_pct,
        "attrition_qty": tidy_number(attrition_qty),
        "buy_quantity": tidy_number(buy),
        "cost_per_assembly": (
            round(cost_per_assembly, 4) if cost_per_assembly is not None else None
        ),
        "extended_line_cost": (
            round(extended_line_cost, 4) if extended_line_cost is not None else None
        ),
        "currency": project.get("currency") or "USD",
    }


def item_flat(row: Any) -> dict[str, Any]:
    """Flatten a ``bom_items`` sqlite row into a dict keyed by field key.

    Real columns come straight off the row; ``extra_fields`` JSON is merged in.
    Computed fields are NOT added here (see :func:`item_public`).
    """
    keys = row.keys() if hasattr(row, "keys") else list(row)
    out: dict[str, Any] = {"id": row["id"], "project_id": row["project_id"]}
    for f in FIELDS:
        if f.db_col:
            out[f.key] = row[f.key] if f.key in keys else None
    extra: dict[str, Any] = {}
    raw = row["extra_fields"] if "extra_fields" in keys else None
    if raw:
        try:
            extra = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
    for key in EXTRA_KEYS:
        out[key] = extra.get(key, "")
    out["created_at"] = row["created_at"] if "created_at" in keys else None
    out["updated_at"] = row["updated_at"] if "updated_at" in keys else None
    return out


def item_public(project: dict[str, Any], row: Any) -> dict[str, Any]:
    """A line item as returned by the JSON API: stored fields + computed fields."""
    flat = item_flat(row)
    flat.update(compute_fields(project, flat))
    return flat


def master_row(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    """Assemble a full 94-column row keyed by Master-BOM header (for CSV export).

    ``item`` may be either an ``item_flat`` dict or an ``item_public`` dict; the
    computed columns are (re)derived here to guarantee they are present.
    """
    computed = compute_fields(project, item)
    out: dict[str, Any] = {}
    for f in FIELDS:
        val = computed.get(f.key, "") if f.computed else item.get(f.key, "")
        out[f.header] = "" if val is None else val
    return out


def approved_parts_row(item: dict[str, Any]) -> dict[str, Any]:
    """Map a line item onto the 12 kicad-parts ``approved_parts.csv`` columns."""
    raw_alts = str(item.get("approved_alternate_mpns") or "").split(",")
    alts = [a.strip() for a in raw_alts if a.strip()]
    dpn = item.get("preferred_dpn") or ""
    note_bits = [str(item.get("comment") or "").strip()]
    if dpn and dpn != "TBD":
        note_bits.append(f"DPN {item.get('preferred_distributor') or ''} {dpn}".strip())
    notes = "; ".join(b for b in note_bits if b)
    return {
        "mpn": item.get("mpn") or "",
        "manufacturer": item.get("manufacturer") or "",
        "datasheet": item.get("datasheet_url") or "",
        "alt_mpn_1": alts[0] if len(alts) > 0 else "",
        "alt_mpn_2": alts[1] if len(alts) > 1 else "",
        "lifecycle_status": item.get("lifecycle_status") or "",
        "preferred_distributor": item.get("preferred_distributor") or "",
        "min_qty_price": item.get("unit_cost") if item.get("unit_cost") not in (None, "") else "",
        "package": item.get("package_size") or "",
        "value": item.get("value") or "",
        "function": item.get("description") or "",
        "notes": notes,
    }
