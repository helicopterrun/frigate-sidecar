from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frigate_sidecar.bom_schema import (
    APPROVED_PARTS_COLUMNS,
    MASTER_BOM_COLUMNS,
    category_from_designator,
    compute_fields,
)
from frigate_sidecar.config import FrigateSection, Settings, SidecarSection
from frigate_sidecar.server import create_app


@pytest.fixture
def client(frigate_db_path: Path, sidecar_db_path: Path, tmp_path: Path) -> TestClient:
    fake_config = tmp_path / "frigate-config.yml"
    fake_config.write_text("cameras: {}\n")
    settings = Settings(
        frigate=FrigateSection(
            base_url="http://frigate.test:5000",
            config_path=fake_config,
            db_path=frigate_db_path,
        ),
        sidecar=SidecarSection(db_path=sidecar_db_path, bind_port=5001),
    )
    return TestClient(create_app(settings))


def _make_project(client: TestClient, **kw: object) -> str:
    payload = {"project_name": "Generic LED Driver", "build_quantity": 10, "attrition_pct": 0.05}
    payload.update(kw)
    r = client.post("/bom/projects", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["project"]["slug"]


# --- pages -------------------------------------------------------------------


def test_index_renders_empty(client: TestClient) -> None:
    r = client.get("/bom")
    assert r.status_code == 200
    assert "No BOM projects yet" in r.text


def test_project_page_renders(client: TestClient) -> None:
    slug = _make_project(client)
    r = client.get(f"/bom/{slug}")
    assert r.status_code == 200
    assert "Generic LED Driver" in r.text
    # Advanced fields section exposes an extra Master-BOM column.
    assert "Voltage Rating" in r.text


def test_unknown_project_404(client: TestClient) -> None:
    assert client.get("/bom/nope").status_code == 404
    assert client.get("/bom/nope/items").status_code == 404


# --- projects ----------------------------------------------------------------


def test_create_project_appears_in_list(client: TestClient) -> None:
    slug = _make_project(client, project_name="Tri-wavelength Emitter")
    assert slug == "tri-wavelength-emitter"
    listed = client.get("/bom/projects").json()["projects"]
    assert [p["slug"] for p in listed] == [slug]


def test_slug_uniqueness(client: TestClient) -> None:
    a = _make_project(client, project_name="Board X")
    b = _make_project(client, project_name="Board X")
    assert a == "board-x"
    assert b == "board-x-2"


def test_config_update_and_delete_cascade(client: TestClient) -> None:
    slug = _make_project(client)
    client.post(f"/bom/{slug}/items", json={"designator": "R1", "mpn": "X"})
    r = client.put(f"/bom/{slug}/config", json={"project_name": "Renamed", "build_quantity": 3})
    assert r.status_code == 200
    assert r.json()["project"]["project_name"] == "Renamed"
    assert client.delete(f"/bom/{slug}").status_code == 200
    assert client.get("/bom/projects").json()["projects"] == []


# --- items -------------------------------------------------------------------


def test_add_item_computes_quantities(client: TestClient) -> None:
    slug = _make_project(client, build_quantity=10, attrition_pct=0.05)
    r = client.post(
        f"/bom/{slug}/items",
        json={
            "designator": "C37, C51, C52",
            "qty_per_assembly": 3,
            "part_category": "Capacitor",
            "unit_cost": 0.10,
            "moq": 10,
        },
    )
    assert r.status_code == 201, r.text
    item = r.json()["item"]
    # 3/assembly * 10 builds = 30 required; +5% attrition rounds to 2 -> 32 buy.
    assert item["total_required"] == 30
    assert item["attrition_qty"] == 2
    assert item["buy_quantity"] == 32
    assert item["cost_per_assembly"] == pytest.approx(0.30)
    assert item["extended_line_cost"] == pytest.approx(3.20)
    assert item["item_no"] == 1


def test_edit_item_roundtrips_advanced_field(client: TestClient) -> None:
    slug = _make_project(client)
    item_id = client.post(f"/bom/{slug}/items", json={"designator": "U1"}).json()["item"]["id"]
    r = client.put(
        f"/bom/{slug}/items/{item_id}",
        json={"designator": "U1", "mpn": "TPS54302", "extra": {"voltage_rating": "28V"}},
    )
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["mpn"] == "TPS54302"
    assert item["voltage_rating"] == "28V"


def test_delete_item(client: TestClient) -> None:
    slug = _make_project(client)
    item_id = client.post(f"/bom/{slug}/items", json={"designator": "R1"}).json()["item"]["id"]
    assert client.delete(f"/bom/{slug}/items/{item_id}").status_code == 200
    assert client.delete(f"/bom/{slug}/items/{item_id}").status_code == 404
    assert client.get(f"/bom/{slug}/items").json()["items"] == []


def test_item_validation(client: TestClient) -> None:
    slug = _make_project(client)
    assert client.post(f"/bom/{slug}/items", json={"populate": "MAYBE"}).status_code == 422
    assert client.post(f"/bom/{slug}/items", json={"qty_per_assembly": -1}).status_code == 422
    # Unknown top-level keys are rejected (advanced fields must go under `extra`).
    assert client.post(f"/bom/{slug}/items", json={"bogus": 1}).status_code == 422


# --- KiCad import ------------------------------------------------------------

# Real column shape from the workbook's KiCad_Import sheet.
KICAD_CSV = (
    "Id,Designator,Footprint,Quantity,Designation,Supplier and ref\n"
    "1,C33,C_0402_1005Metric,1,22pF NP0,\n"
    "5,\"C37, C51, C52\",C_1206_3216Metric,3,22uF/25V,\n"
    "9,R5,R_0402_1005Metric,1,10k,\n"
    "12,U1,SOT-23-6,1,TPS54302,\n"
)


def test_kicad_import_maps_and_infers(client: TestClient) -> None:
    slug = _make_project(client)
    r = client.post(f"/bom/{slug}/import/kicad", json={"csv_text": KICAD_CSV})
    assert r.status_code == 200
    assert r.json()["added"] == 4

    items = {i["designator"]: i for i in client.get(f"/bom/{slug}/items").json()["items"]}
    assert items["C33"]["value"] == "22pF NP0"
    assert items["C33"]["footprint"] == "C_0402_1005Metric"
    assert items["C33"]["part_category"] == "Capacitor"
    assert items["C33"]["source"] == "Imported from KiCad CSV"
    # Grouped designators keep their quantity; category still inferred from first ref.
    assert items["C37, C51, C52"]["qty_per_assembly"] == 3
    assert items["R5"]["part_category"] == "Resistor"
    assert items["U1"]["part_category"] == "IC"


def test_kicad_import_rejects_unrecognized(client: TestClient) -> None:
    slug = _make_project(client)
    r = client.post(f"/bom/{slug}/import/kicad", json={"csv_text": "foo,bar\n1,2\n"})
    assert r.status_code == 400


# --- exports -----------------------------------------------------------------


def test_master_csv_has_all_94_columns_in_order(client: TestClient) -> None:
    slug = _make_project(client)
    client.post(
        f"/bom/{slug}/items",
        json={"designator": "C1", "value": "100nF", "mpn": "ABC", "extra": {"rohs": "Y"}},
    )
    text = client.get(f"/bom/{slug}/export.csv").text
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == MASTER_BOM_COLUMNS
    assert len(rows[0]) == 94
    record = dict(zip(rows[0], rows[1], strict=True))
    assert record["Value"] == "100nF"
    assert record["MPN"] == "ABC"
    assert record["RoHS"] == "Y"  # advanced field survives the round-trip
    assert record["Currency"] == "USD"  # computed from project config


def test_approved_parts_csv(client: TestClient) -> None:
    slug = _make_project(client)
    # Installed + real MPN -> included.
    client.post(
        f"/bom/{slug}/items",
        json={
            "designator": "U1", "mpn": "TPS54302", "manufacturer": "TI",
            "value": "buck", "package_size": "SOT-23-6", "datasheet_url": "http://d",
            "populate": "YES",
        },
    )
    # Missing MPN (TBD) -> excluded.
    client.post(f"/bom/{slug}/items", json={"designator": "C1", "populate": "YES"})
    # DNP -> excluded even with an MPN.
    client.post(f"/bom/{slug}/items", json={"designator": "R1", "mpn": "RRR", "populate": "DNP"})

    text = client.get(f"/bom/{slug}/approved_parts.csv").text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0].keys()) == APPROVED_PARTS_COLUMNS
    assert len(rows) == 1
    assert rows[0]["mpn"] == "TPS54302"
    assert rows[0]["manufacturer"] == "TI"
    assert rows[0]["package"] == "SOT-23-6"


def test_export_json_shape(client: TestClient) -> None:
    slug = _make_project(client)
    client.post(f"/bom/{slug}/items", json={"designator": "R1", "mpn": "X"})
    body = client.get(f"/bom/{slug}/export.json").json()
    assert body["columns"] == MASTER_BOM_COLUMNS
    assert len(body["items"]) == 1
    assert "rollup" in body
    assert body["project"]["slug"] == slug


# --- rollup ------------------------------------------------------------------


def test_rollup_counts(client: TestClient) -> None:
    slug = _make_project(client)
    client.post(
        f"/bom/{slug}/items",
        json={"designator": "U1", "mpn": "X", "unit_cost": 1.0, "populate": "YES",
              "review_status": "Approved"},
    )
    client.post(f"/bom/{slug}/items", json={"designator": "C1", "populate": "YES"})  # missing MPN
    client.post(f"/bom/{slug}/items", json={"designator": "R1", "mpn": "Y", "populate": "DNP"})

    rollup = client.get(f"/bom/{slug}/items").json()["rollup"]
    assert rollup["total_lines"] == 3
    assert rollup["populated_lines"] == 2
    assert rollup["dnp_lines"] == 1
    assert rollup["missing_mpn_lines"] == 1  # C1 installed with no MPN
    assert rollup["needs_review_lines"] == 2  # C1 + R1 default "Needs Review"


# --- pure helpers ------------------------------------------------------------


def test_category_from_designator() -> None:
    assert category_from_designator("C33") == "Capacitor"
    assert category_from_designator("LED2") == "LED"  # longest-prefix wins over "L"
    assert category_from_designator("SW1") == "Switch"
    assert category_from_designator("C37, C51") == "Capacitor"
    assert category_from_designator("") == ""


def test_compute_fields_quantity_check() -> None:
    proj = {"build_quantity": 1, "attrition_pct": 0.0, "currency": "USD"}
    ok = compute_fields(proj, {"designator": "C1, C2", "qty_per_assembly": 2})
    assert ok["quantity_check"] == "OK"
    mismatch = compute_fields(proj, {"designator": "C1, C2", "qty_per_assembly": 5})
    assert mismatch["quantity_check"] == "CHECK"
