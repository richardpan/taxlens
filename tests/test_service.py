"""Service-layer tests: import → list → whatif → override, all against a temp DB."""
from decimal import Decimal
from pathlib import Path

import pytest

from taxlens.db import make_sessionmaker
from taxlens.service import TaxLensService


@pytest.fixture
def svc(tmp_path) -> TaxLensService:
    db = tmp_path / "taxlens-test.sqlite"
    return TaxLensService(make_sessionmaker(db))


def _yaml_fixture_path() -> Path:
    return Path(__file__).parent / "fixtures" / "returns" / "mfj_2024_basic.yaml"


def test_import_yaml_then_list(svc):
    row, result, warnings = svc.import_file(_yaml_fixture_path())
    assert row.tax_year == 2024
    assert result.total_tax == Decimal("34117.00")
    listings = svc.list_returns()
    assert len(listings) == 1
    assert listings[0]["tax_year"] == 2024


def test_idempotent_reimport(svc):
    svc.import_file(_yaml_fixture_path())
    svc.import_file(_yaml_fixture_path())
    assert len(svc.list_returns()) == 1, "same hash should replace, not duplicate"


def test_whatif_recompute(svc):
    row, _, _ = svc.import_file(_yaml_fixture_path())
    out = svc.whatif(row.id, {"wages": "260000"})
    assert out is not None
    assert Decimal(out["whatif"]["total_tax"]) > Decimal(out["original"]["total_tax"])
    # Engine never mutates the stored row on what-if
    full = svc.get_return(row.id)
    assert Decimal(full["return"]["wages"]) == Decimal(240000)


def test_override_persists_and_recomputes(svc):
    row, _, _ = svc.import_file(_yaml_fixture_path())
    assert svc.commit_override(row.id, "wages", "260000", "spouse raise")
    full = svc.get_return(row.id)
    assert Decimal(full["return"]["wages"]) == Decimal(260000)
    assert full["overrides"][0]["field"] == "wages"
    assert full["overrides"][0]["reason"] == "spouse raise"


def test_get_by_year(svc):
    svc.import_file(_yaml_fixture_path())
    out = svc.get_by_year(2024)
    assert out is not None and out["tax_year"] == 2024
    assert svc.get_by_year(1999) is None


def test_delete(svc):
    row, _, _ = svc.import_file(_yaml_fixture_path())
    assert svc.delete_return(row.id)
    assert svc.list_returns() == []
