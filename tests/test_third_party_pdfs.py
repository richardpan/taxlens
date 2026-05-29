"""Golden tests for third-party tax software PDF imports (TurboTax, H&R Block,
FreeTaxUSA). These lock in importer compatibility with vendor-specific quirks:

  - Canonical IRS line phrasing (line 1a says "Total amount from Form(s) W-2,
    box 1", NOT "Wages")
  - Explicit 'Filing Status: X' markers instead of checkbox indicators
  - Column-split layouts where pdfplumber may emit label and amount on
    adjacent lines
  - Cover/summary pages preceding the actual Form 1040
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taxlens.importers.pdf import import_pdf
from taxlens.models import FilingStatus

from tests.third_party_pdfs import (
    ThirdPartyReturn,
    make_freetaxusa_1040,
    make_hrblock_1040,
    make_turbotax_1040,
)


@pytest.fixture
def base_return() -> ThirdPartyReturn:
    return ThirdPartyReturn(
        tax_year=2023,
        filing_status_label="Married Filing Jointly",
        wages=Decimal(120_000),
        interest=Decimal(450),
        qual_div=Decimal(800),
        ord_div=Decimal(1_000),
        withholding=Decimal(18_500),
        qualifying_children=2,
        agi=Decimal(121_450),
        taxable_income=Decimal(92_250),
        total_tax=Decimal(11_400),
    )


def test_turbotax_pdf_imports_cleanly(tmp_path: Path, base_return: ThirdPartyReturn) -> None:
    path = tmp_path / "turbotax_2023.pdf"
    make_turbotax_1040(path, base_return)

    imp = import_pdf(path)
    assert imp.ret.tax_year == 2023
    assert imp.ret.filing_status == FilingStatus.MFJ
    assert imp.ret.wages == Decimal(120_000)
    assert imp.ret.interest_income == Decimal(450)
    assert imp.ret.qualified_dividends == Decimal(800)
    assert imp.ret.ordinary_dividends == Decimal(1_000)
    assert imp.ret.federal_withholding == Decimal(18_500)
    assert imp.ret.reported_total_tax == Decimal(11_400)


def test_hrblock_pdf_imports_cleanly(tmp_path: Path, base_return: ThirdPartyReturn) -> None:
    path = tmp_path / "hrblock_2023.pdf"
    make_hrblock_1040(path, base_return)

    imp = import_pdf(path)
    assert imp.ret.tax_year == 2023
    assert imp.ret.filing_status == FilingStatus.MFJ
    assert imp.ret.wages == Decimal(120_000)
    assert imp.ret.interest_income == Decimal(450)
    assert imp.ret.qualified_dividends == Decimal(800)
    assert imp.ret.ordinary_dividends == Decimal(1_000)
    assert imp.ret.federal_withholding == Decimal(18_500)
    assert imp.ret.reported_total_tax == Decimal(11_400)


def test_freetaxusa_pdf_imports_cleanly(tmp_path: Path, base_return: ThirdPartyReturn) -> None:
    path = tmp_path / "freetaxusa_2023.pdf"
    make_freetaxusa_1040(path, base_return)

    imp = import_pdf(path)
    assert imp.ret.tax_year == 2023
    assert imp.ret.filing_status == FilingStatus.MFJ
    assert imp.ret.wages == Decimal(120_000)
    assert imp.ret.interest_income == Decimal(450)
    assert imp.ret.qualified_dividends == Decimal(800)
    assert imp.ret.ordinary_dividends == Decimal(1_000)
    assert imp.ret.federal_withholding == Decimal(18_500)
    assert imp.ret.reported_total_tax == Decimal(11_400)


@pytest.mark.parametrize("label,expected", [
    ("Single", FilingStatus.SINGLE),
    ("Married Filing Jointly", FilingStatus.MFJ),
    ("Married Filing Separately", FilingStatus.MFS),
    ("Head of Household", FilingStatus.HOH),
    ("Qualifying Surviving Spouse", FilingStatus.QSS),
])
def test_filing_status_detection_across_vendors(
    tmp_path: Path, label: str, expected: FilingStatus
) -> None:
    """All vendor formats must correctly parse all 5 filing statuses via the
    explicit 'Filing Status: X' marker, regardless of which other status labels
    appear elsewhere on the form."""
    r = ThirdPartyReturn(
        tax_year=2023,
        filing_status_label=label,
        wages=Decimal(50_000),
        withholding=Decimal(5_000),
        agi=Decimal(50_000),
        taxable_income=Decimal(35_400),
        total_tax=Decimal(4_000),
    )
    for vendor, make in [("tt", make_turbotax_1040), ("hr", make_hrblock_1040),
                         ("fr", make_freetaxusa_1040)]:
        path = tmp_path / f"{vendor}_{label.replace(' ', '_')}.pdf"
        make(path, r)
        imp = import_pdf(path)
        assert imp.ret.filing_status == expected, (
            f"{vendor} failed for {label!r}: got {imp.ret.filing_status}"
        )


def test_year_detection_handles_third_party_headers(tmp_path: Path) -> None:
    """Each vendor uses a slightly different header style for the year — make
    sure all of them are detected."""
    for vendor, make, year in [
        ("turbotax", make_turbotax_1040, 2019),
        ("hrblock", make_hrblock_1040, 2021),
        ("freetaxusa", make_freetaxusa_1040, 2017),
    ]:
        r = ThirdPartyReturn(
            tax_year=year,
            filing_status_label="Single",
            wages=Decimal(40_000),
            withholding=Decimal(3_500),
            agi=Decimal(40_000),
            taxable_income=Decimal(27_400),
            total_tax=Decimal(3_100),
        )
        path = tmp_path / f"{vendor}_{year}.pdf"
        make(path, r)
        imp = import_pdf(path)
        assert imp.ret.tax_year == year, f"{vendor} {year}: got {imp.ret.tax_year}"
