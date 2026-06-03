"""Synthetic round-trip property test for the PDF importer parser.

Builds 1040-shaped TEXT blobs (no actual PDF rendering — that's
covered by the synthetic_pdf-based tests), feeds them to the
importer's text extractor, and asserts the recovered values match
the ones we put in. This catches importer regressions for free
without any real tax-return PDFs ever being involved.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from taxlens.importers.pdf import _extract_fields


def _money(d: Decimal) -> str:
    """Render a Decimal as an IRS-style amount string with thousands
    separators and no trailing zeros for whole-dollar values."""
    if d == d.to_integral_value():
        return f"{int(d):,}"
    return f"{d:,.2f}"


def _render_1040(
    *,
    tax_year: int,
    filing_status_label: str,
    wages: Decimal,
    interest: Decimal,
    qual_div: Decimal,
    ord_div: Decimal,
    total_tax_reported: Decimal | None,
    withholding: Decimal,
) -> str:
    """Render a Form 1040-shaped text blob mirroring the layout of
    ``tests/synthetic_pdf.py`` so the importer's text extractor can
    parse it as if it had come from pdfplumber's default stream."""
    lines = [
        f"Form 1040 ({tax_year}) — U.S. Individual Income Tax Return",
        "",
        f"Filing Status: [X] {filing_status_label}",
        "",
        f"Line 1a   Wages, salaries, tips ........................ {_money(wages)}",
    ]
    if interest:
        lines.append(f"Line 2b   Taxable interest ............................ {_money(interest)}")
    if qual_div:
        lines.append(f"Line 3a   Qualified dividends ......................... {_money(qual_div)}")
    if ord_div:
        lines.append(f"Line 3b   Ordinary dividends .......................... {_money(ord_div)}")
    if total_tax_reported is not None:
        lines.append(f"Line 24   Total tax ................................... {_money(total_tax_reported)}")
    if withholding:
        lines.append(f"Line 25a  Federal income tax withheld from W-2 ........ {_money(withholding)}")
    return "\n".join(lines)


# Decimal strategies — bounded to plausible 1040 values, whole-dollar
# (matches IRS form rounding) so the round-trip is exact.
_amount = st.integers(min_value=0, max_value=999_999).map(Decimal)
_amount_nonzero = st.integers(min_value=1, max_value=999_999).map(Decimal)


@settings(max_examples=50, deadline=None)
@given(
    wages=_amount_nonzero,
    interest=_amount,
    qual_div=_amount,
    ord_div=_amount,
    withholding=_amount,
    total_tax=_amount,
)
def test_roundtrip_recovers_extracted_values(
    wages: Decimal,
    interest: Decimal,
    qual_div: Decimal,
    ord_div: Decimal,
    withholding: Decimal,
    total_tax: Decimal,
) -> None:
    """For any plausible set of 1040 line values, rendering them into a
    1040-shaped text blob and feeding that through ``_extract_fields``
    must recover the same values byte-for-byte."""
    text = _render_1040(
        tax_year=2024,
        filing_status_label="Single",
        wages=wages,
        interest=interest,
        qual_div=qual_div,
        ord_div=ord_div,
        total_tax_reported=total_tax,
        withholding=withholding,
    )
    fields, _children, _warnings = _extract_fields([text])

    assert fields.get("wages") == wages
    if interest:
        assert fields.get("interest_income") == interest
    if qual_div:
        assert fields.get("qualified_dividends") == qual_div
    if ord_div:
        assert fields.get("ordinary_dividends") == ord_div
    if withholding:
        assert fields.get("federal_withholding") == withholding
    # total_tax_reported is always emitted in this fixture.
    assert fields.get("total_tax_reported") == total_tax


@settings(max_examples=20, deadline=None)
@given(amount=st.integers(min_value=0, max_value=99_999_999).map(Decimal))
def test_roundtrip_handles_full_amount_range(amount: Decimal) -> None:
    """Wages can legitimately span $0 to ~$100M; the parser must
    handle the full plausible range without losing precision or
    confusing thousands separators."""
    text = _render_1040(
        tax_year=2024,
        filing_status_label="Single",
        wages=amount if amount > 0 else Decimal(1),
        interest=amount,
        qual_div=Decimal(0),
        ord_div=Decimal(0),
        total_tax_reported=None,
        withholding=Decimal(0),
    )
    fields, _children, _warnings = _extract_fields([text])
    if amount:
        assert fields.get("interest_income") == amount


def test_roundtrip_zero_values_are_omitted_not_misparsed() -> None:
    """When a line is omitted from the rendered form (zero value),
    the extractor must NOT recover a phantom value for that field."""
    text = _render_1040(
        tax_year=2024,
        filing_status_label="Single",
        wages=Decimal("50000"),
        interest=Decimal(0),  # omitted from rendering
        qual_div=Decimal(0),
        ord_div=Decimal(0),
        total_tax_reported=None,
        withholding=Decimal(0),
    )
    fields, _children, _warnings = _extract_fields([text])
    assert fields.get("wages") == Decimal("50000")
    assert "interest_income" not in fields
    assert "qualified_dividends" not in fields
    assert "ordinary_dividends" not in fields


def test_roundtrip_decimal_cents_preserved() -> None:
    """Some vendors render values with cents (e.g. $1,234.56). Round-trip
    must preserve the fractional component exactly."""
    text = _render_1040(
        tax_year=2024,
        filing_status_label="Single",
        wages=Decimal("100000.55"),
        interest=Decimal("0"),
        qual_div=Decimal("0"),
        ord_div=Decimal("0"),
        total_tax_reported=Decimal("13841.50"),
        withholding=Decimal("0"),
    )
    fields, _children, _warnings = _extract_fields([text])
    assert fields.get("wages") == Decimal("100000.55")
    assert fields.get("total_tax_reported") == Decimal("13841.50")
