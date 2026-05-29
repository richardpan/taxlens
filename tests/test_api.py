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

def test_import_returns_422_with_diagnostic_on_bad_pdf(client) -> None:
    """A garbage file should yield a useful 422 (not a bare 500), with
    enough info in `detail` for the user to know what went wrong."""
    r = client.post(
        "/api/returns/import",
        files={"file": ("not-a-pdf.pdf", b"%PDF-1.4 garbage not really a pdf", "application/pdf")},
    )
    assert r.status_code in (400, 422), r.text
    detail = r.json().get("detail", "")
    assert detail, "expected non-empty detail field"
    # Should mention the filename so the user can correlate with their input.
    assert "not-a-pdf.pdf" in detail or "Could not" in detail or "tax year" in detail.lower()


def test_debug_extract_handles_garbage_pdf_gracefully(client) -> None:
    """The debug endpoint should never raise — it returns an `error` field
    inside a 200 response so the user can always inspect what's happening."""
    r = client.post(
        "/api/debug/extract",
        files={"file": ("garbage.pdf", b"%PDF-1.4 nonsense", "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "pdf"
    # Either it parsed (unlikely) or returned an error string.
    assert "error" in body or "pages" in body


def test_delete_return_via_api(client) -> None:
    """End-to-end: import a return, list it, delete it, confirm gone."""
    yaml_path = _fixture_yaml()
    with yaml_path.open("rb") as f:
        r = client.post(
            "/api/returns/import",
            files={"file": ("mfj_2024_basic.yaml", f, "application/yaml")},
        )
    assert r.status_code == 200
    rid = r.json()["id"]

    assert any(x["id"] == rid for x in client.get("/api/returns").json())

    r = client.delete(f"/api/returns/{rid}")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}

    assert not any(x["id"] == rid for x in client.get("/api/returns").json())

    # Re-delete should 404.
    assert client.delete(f"/api/returns/{rid}").status_code == 404
