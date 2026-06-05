"""Standalone W-2 PDF import tests.

Covers the detection helper (_is_w2_only_pdf) and the service-level
merge path (_attach_w2). Synthetic text fixtures only — no real PDFs.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taxlens.importers import Imported
from taxlens.importers.pdf._core import _is_w2_only_pdf
from taxlens.models import FilingStatus, Return
from taxlens.service import TaxLensService


# ─── detection ──────────────────────────────────────────────────────────────


def test_w2_only_pdf_detected_from_letterhead():
    pages = [
        "2024 Wage and Tax Statement\nEmployer: Acme Corp\n"
        "Box 1 80,000.00\n"
        "Box 12a D 23,000.00\n"
        "Box 12b W 4,150.00\n"
    ]
    assert _is_w2_only_pdf(pages) is True


def test_bundled_1040_with_w2_not_classified_as_standalone():
    pages = [
        "Form 1040 (2024) — U.S. Individual Income Tax Return\n"
        "Wages, salaries, tips ... 1a  80,000\n"
        "Adjusted gross income .... 11  78,000\n",
        "Wage and Tax Statement\nBox 12a D 23,000.00\n",
    ]
    assert _is_w2_only_pdf(pages) is False


def test_no_w2_marker_not_classified():
    pages = ["Form 1040 (2024)\nWages 80,000\n"]
    assert _is_w2_only_pdf(pages) is False


# ─── merge path ─────────────────────────────────────────────────────────────


@pytest.fixture
def svc(tmp_path: Path) -> TaxLensService:
    return TaxLensService.open(tmp_path / "test.db")


def test_w2_merge_into_existing_return(svc: TaxLensService):
    base = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("80000"),
    )
    svc.import_return(base, source="manual")

    w2 = Imported(
        ret=Return(
            tax_year=2024,
            filing_status=FilingStatus.SINGLE,
            traditional_401k_contributions=Decimal("23000"),
            hsa_contributions=Decimal("4150"),
            w2_data_present=True,
        ),
        source="pdf-w2",
        source_hash="testhash1",
        source_filename="w2_acme.pdf",
        warnings=[],
    )
    row, result, _ = svc._store(w2)
    merged = svc.get_by_year(2024)
    assert merged is not None
    ret = merged["return"]
    assert Decimal(ret["traditional_401k_contributions"]) == Decimal("23000")
    assert Decimal(ret["hsa_contributions"]) == Decimal("4150")
    assert ret["w2_data_present"] is True
    # Merge must not corrupt the base 1040 wage figure.
    assert Decimal(ret["wages"]) == Decimal("80000")


def test_w2_merge_is_additive_for_multiple_jobs(svc: TaxLensService):
    """A spouse W-2 + employee W-2 for the same year should sum."""
    base = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ, wages=Decimal("200000"),
    )
    svc.import_return(base, source="manual")

    for amt, h in [("15000", "h1"), ("8000", "h2")]:
        svc._store(Imported(
            ret=Return(
                tax_year=2024,
                filing_status=FilingStatus.SINGLE,
                traditional_401k_contributions=Decimal(amt),
                w2_data_present=True,
            ),
            source="pdf-w2",
            source_hash=h,
            source_filename=f"w2_{h}.pdf",
            warnings=[],
        ))
    merged = svc.get_by_year(2024)
    assert merged is not None
    assert Decimal(merged["return"]["traditional_401k_contributions"]) == Decimal("23000")


def test_w2_without_existing_return_raises(svc: TaxLensService):
    w2 = Imported(
        ret=Return(
            tax_year=2099,  # no return for this year exists
            filing_status=FilingStatus.SINGLE,
            traditional_401k_contributions=Decimal("23000"),
            w2_data_present=True,
        ),
        source="pdf-w2",
        source_hash="orphan",
        source_filename="w2_orphan.pdf",
        warnings=[],
    )
    with pytest.raises(ValueError, match="No existing return found"):
        svc._store(w2)
