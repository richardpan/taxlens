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


def test_w2_with_omb_overrides_narrative_form_1040_mentions():
    """ADP-style W-2 PDFs include an instructions page that narratively
    references 'Form 1040' multiple times. The W-2 OMB number (1545-0029)
    is a strong positive signal that should override prose mentions."""
    pages = [
        "W-2 Wage and Tax Statement 2025\nOMB No. 1545-0029\n"
        "MICROSOFT CORPORATION\nD 23500.00\nW 8550.00\n",
        "Instructions for Employee\nBox 1. Enter this amount on the wages "
        "line of your tax return.\nSee the Form 1040 instructions to "
        "determine if you are required to complete Form 8959.\n",
    ]
    assert _is_w2_only_pdf(pages) is True


# ─── ADP-style multi-copy dedup ─────────────────────────────────────────────


def test_box12_dedup_handles_4_identical_copies():
    """ADP renders 4 identical W-2 copies (Reference / Federal / State /
    City) on a single page. Naive summing across the joined text would
    produce 4× the actual values. The dedup parser fingerprints each
    region and counts identical copies once."""
    from taxlens.importers.pdf._core import _extract_w2_box12_dedup

    text = "\n".join([
        "Employee Reference Copy",
        "W-2 Wage and Tax Statement 2025 OMB No. 1545-0029",
        "MICROSOFT CORPORATION",
        "D 23500.00",
        "W 8550.00",
        "Federal Filing Copy",
        "W-2 Wage and Tax Statement 2025 OMB No. 1545-0029",
        "MICROSOFT CORPORATION",
        "D 23500.00",
        "W 8550.00",
        "State Filing Copy",
        "W-2 Wage and Tax Statement 2025 OMB No. 1545-0029",
        "MICROSOFT CORPORATION",
        "D 23500.00",
        "W 8550.00",
        "City or Local Filing Copy",
        "W-2 Wage and Tax Statement 2025 OMB No. 1545-0029",
        "MICROSOFT CORPORATION",
        "D 23500.00",
        "W 8550.00",
    ])
    out = _extract_w2_box12_dedup(text)
    # 4 identical copies → counted once, not summed to 4× $23,500.
    assert out["traditional_401k_contributions"] == Decimal("23500.00")
    assert out["hsa_contributions"] == Decimal("8550.00")


def test_box12_dedup_sums_two_distinct_w2s():
    """Two distinct W-2s for the same person (multiple jobs / spouse)
    should fingerprint differently and SUM, not dedup."""
    from taxlens.importers.pdf._core import _extract_w2_box12_dedup

    text = "\n".join([
        "W-2 Wage and Tax Statement 2024 OMB No. 1545-0029",
        "ACME CORP",
        "D 15000.00",
        "W 3000.00",
        "W-2 Wage and Tax Statement 2024 OMB No. 1545-0029",
        "BETA LLC",
        "D 8000.00",
        "W 1500.00",
    ])
    out = _extract_w2_box12_dedup(text)
    assert out["traditional_401k_contributions"] == Decimal("23000.00")
    assert out["hsa_contributions"] == Decimal("4500.00")


def test_w2_year_detection_handles_w2_specific_anchors():
    """W-2 PDFs use 'Wage and Tax Statement YYYY' or 'YYYY W-2' rather
    than 1040-style anchors; the dedicated detector picks them up."""
    from taxlens.importers.pdf._core import _detect_w2_year

    assert _detect_w2_year(["W-2 Wage and Tax Statement 2025"]) == 2025
    assert _detect_w2_year(["2024 W-2 and EARNINGS SUMMARY"]) == 2024
    assert _detect_w2_year(["OMB No. 1545-0029  Tax Year 2023"]) == 2023


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
