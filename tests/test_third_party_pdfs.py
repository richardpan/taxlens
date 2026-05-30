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
    make_fillable_offset_1040,
    make_freetaxusa_1040,
    make_freetaxusa_realistic_1040,
    make_freetaxusa_summary_mismatch_1040,
    make_hrblock_1040,
    make_hrblock_packed_1040,
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


def test_freetaxusa_realistic_column_split_imports_cleanly(
    tmp_path: Path, base_return: ThirdPartyReturn
) -> None:
    """Realistic FreeTaxUSA layout: summary cover page with friendly labels,
    then a column-split 1040 facsimile where amounts live on a separate line
    from labels with noise lines (dot-leaders, '(see instructions)', 'Attach
    Schedule B') in between. This is what's been silently breaking income
    extraction in user uploads."""
    path = tmp_path / "freetaxusa_realistic_2023.pdf"
    make_freetaxusa_realistic_1040(path, base_return)

    imp = import_pdf(path)
    assert imp.ret.tax_year == 2023
    assert imp.ret.filing_status == FilingStatus.MFJ
    assert imp.ret.wages == Decimal(120_000), f"got {imp.ret.wages}"
    assert imp.ret.interest_income == Decimal(450), f"got {imp.ret.interest_income}"
    assert imp.ret.qualified_dividends == Decimal(800)
    assert imp.ret.ordinary_dividends == Decimal(1_000)
    assert imp.ret.federal_withholding == Decimal(18_500)
    assert imp.ret.reported_total_tax == Decimal(11_400)


def test_freetaxusa_summary_with_wrong_numbers_is_ignored(
    tmp_path: Path, base_return: ThirdPartyReturn
) -> None:
    """Page 1 has a FreeTaxUSA-style summary with INFLATED values; page 2 has
    the real Form 1040 (with OMB number + IRS markers). The importer must
    extract from the real form and ignore the lying summary."""
    path = tmp_path / "freetaxusa_summary_lies.pdf"
    make_freetaxusa_summary_mismatch_1040(path, base_return)

    imp = import_pdf(path)
    assert imp.ret.tax_year == 2023
    assert imp.ret.filing_status == FilingStatus.MFJ
    assert imp.ret.wages == Decimal(120_000), f"got {imp.ret.wages}"
    assert imp.ret.interest_income == Decimal(450)
    assert imp.ret.qualified_dividends == Decimal(800)
    assert imp.ret.ordinary_dividends == Decimal(1_000)
    assert imp.ret.federal_withholding == Decimal(18_500)
    assert imp.ret.reported_total_tax == Decimal(11_400)
    assert any("Skipped" in w and "summary" in w.lower() for w in imp.warnings), imp.warnings


def test_hrblock_packed_layout_with_cover_and_summary(tmp_path: Path) -> None:
    """Mirrors a real H&R Block 2020 user PDF (PII redacted, amounts mocked):

      - Page 1 is a "Filing Checklist" cover (must be skipped)
      - Page 2 is a "Quick Summary" with rounded totals that differ from
        the real form (must be skipped — extracting from it would poison
        the dashboard)
      - Pages 3+ are real Form 1040 pages with the packed-line layout
        ``1 Wages, salaries, tips, etc. ... 1 176,865`` where the value
        sits directly after a repeated line-number with only a single
        space between them.

    Pre-fix regression: the importer extracted the trailing line number
    (1, 2, 3, 7, 24) as the value because the money regex's ``_is_form_id_digit``
    guard incorrectly flagged the real value as a "form identifier" — the
    preceding char was the last digit of the line number.
    """
    r = ThirdPartyReturn(
        tax_year=2020,
        filing_status_label="Married Filing Jointly",
        wages=Decimal(176_865),
        interest=Decimal(481),
        qual_div=Decimal(1_374),
        ord_div=Decimal(2_223),
        withholding=Decimal(24_420),
        agi=Decimal(242_557),
        taxable_income=Decimal(217_601),
        total_tax=Decimal(39_506),
    )
    path = tmp_path / "hrblock_packed_2020.pdf"
    make_hrblock_packed_1040(path, r)

    imp = import_pdf(path)
    assert imp.ret.tax_year == 2020
    assert imp.ret.filing_status == FilingStatus.MFJ
    assert imp.ret.wages == Decimal(176_865), f"wages got {imp.ret.wages}"
    assert imp.ret.interest_income == Decimal(481), f"interest got {imp.ret.interest_income}"
    assert imp.ret.qualified_dividends == Decimal(1_374), f"qdiv got {imp.ret.qualified_dividends}"
    assert imp.ret.ordinary_dividends == Decimal(2_223), f"odiv got {imp.ret.ordinary_dividends}"
    assert imp.ret.federal_withholding == Decimal(24_420), f"wh got {imp.ret.federal_withholding}"
    assert imp.ret.reported_total_tax == Decimal(39_506), f"tot got {imp.ret.reported_total_tax}"
    # Cover + quick-summary pages should both have been skipped (the wrong
    # totals from the summary must NOT show up anywhere in the result).
    assert any("Skipped" in w for w in imp.warnings), imp.warnings


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


def test_fillable_offset_layout_recovered_by_loose_layout(tmp_path: Path, base_return: ThirdPartyReturn) -> None:
    """Regression: real fillable-form PDFs render label and user-entered
    value with a ~6pt vertical offset. Before the loose-tolerance layout
    extraction, every money field came back as $0 because pdfplumber's
    default extractor split the offset rows. The loose-layout pass must
    merge them back so all key fields parse correctly."""
    p = tmp_path / "fillable_offset_2023.pdf"
    make_fillable_offset_1040(p, base_return)

    imp = import_pdf(p)
    assert imp.ret.tax_year == 2023
    assert imp.ret.wages == Decimal(120_000)
    assert imp.ret.interest_income == Decimal(450)
    assert imp.ret.qualified_dividends == Decimal(800)
    assert imp.ret.ordinary_dividends == Decimal(1_000)
    assert imp.ret.federal_withholding == Decimal(18_500)
    assert imp.ret.reported_total_tax == Decimal(11_400)
    assert any("Layout-aware extraction recovered" in w for w in imp.warnings), \
        f"expected a layout-recovery warning, got: {imp.warnings}"
