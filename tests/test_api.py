"""FastAPI end-to-end smoke test using httpx TestClient.

Uses an isolated temp DB to avoid touching the user's real ~/.taxlens.
"""
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # Point the api module at a fresh DB before it's imported.
    db = tmp_path / "api-test.sqlite"
    monkeypatch.setenv("TAXLENS_DB", str(db))
    # Force-reimport so the module-level service binds to the env-overridden DB.
    import importlib
    import taxlens.db as db_mod
    import taxlens.api as api_mod
    importlib.reload(db_mod)
    importlib.reload(api_mod)
    return TestClient(api_mod.app)


def _fixture_yaml() -> Path:
    return Path(__file__).parent / "fixtures" / "returns" / "mfj_2024_basic.yaml"


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_import_list_whatif_flow(client):
    # Import the YAML fixture via the API.
    with _fixture_yaml().open("rb") as f:
        r = client.post("/api/returns/import", files={"file": ("mfj_2024_basic.yaml", f, "application/yaml")})
    assert r.status_code == 200, r.text
    body = r.json()
    return_id = body["id"]
    assert body["tax_year"] == 2024
    assert Decimal(body["result"]["total_tax"]) == Decimal("34117.00")

    # List
    listings = client.get("/api/returns").json()
    assert len(listings) == 1

    # Detail
    detail = client.get(f"/api/returns/{return_id}").json()
    assert detail["return"]["wages"] == "240000"

    # What-if recompute
    wif = client.post(f"/api/returns/{return_id}/whatif", json={"wages": "300000"}).json()
    assert Decimal(wif["whatif"]["total_tax"]) > Decimal(wif["original"]["total_tax"])


def test_static_ui_mounted(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TaxLens" in r.text


def test_pdf_round_trip_via_api(client, tmp_path):
    from tests.synthetic_pdf import make_1040_pdf
    pdf = tmp_path / "api_round_trip.pdf"
    make_1040_pdf(
        pdf,
        tax_year=2024,
        filing_status_label="Married filing jointly",
        wages=Decimal(240000),
        interest=Decimal(6000),
        withholding=Decimal(40000),
        total_tax_reported=Decimal("34117.00"),
        qualifying_children=2,
    )
    with pdf.open("rb") as f:
        r = client.post("/api/returns/import", files={"file": ("api_round_trip.pdf", f, "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tax_year"] == 2024
    assert Decimal(body["result"]["total_tax"]) == Decimal("34117.00")
    assert body["result"]["reconciliation_delta"] is not None
    assert abs(Decimal(body["result"]["reconciliation_delta"])) <= Decimal("1.00")
