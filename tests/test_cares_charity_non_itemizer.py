"""Tests for the CARES Act / TCDTRA non-itemizer charitable deduction.

* TY2020: above-the-line, $300 cap per return — reduces AGI.
* TY2021: below-the-line, $300 single / $600 MFJ — reduces taxable income only.
* TY2022+: no rule, field is silently ignored.
* Itemizers don't get it (per IRS instructions).
"""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def _basic_2020(**over):
    base = dict(
        tax_year=2020,
        filing_status=FilingStatus.MFJ,
        wages=Decimal(100_000),
    )
    base.update(over)
    return Return(**base)


def test_ty2020_above_line_charity_reduces_agi():
    ret = _basic_2020(charitable_contributions_non_itemizer=Decimal(220))
    r = compute(ret, load_rules(2020))
    # AGI = wages 100,000 - 220 charity = 99,780
    assert r.agi == Decimal("99780.00"), r.agi
    # Standard deduction MFJ 2020 = 24,800 → taxable = 74,980
    assert r.taxable_income == Decimal("74980.00"), r.taxable_income


def test_ty2020_above_line_charity_capped_at_300():
    ret = _basic_2020(charitable_contributions_non_itemizer=Decimal(500))
    r = compute(ret, load_rules(2020))
    # Capped at $300 (MFJ in 2020 does NOT get the doubled $600 cap).
    assert r.agi == Decimal("99700.00"), r.agi


def test_ty2020_itemizer_does_not_get_above_line_charity():
    ret = _basic_2020(
        charitable_contributions_non_itemizer=Decimal(300),
        itemized_deductions=Decimal(30_000),
    )
    r = compute(ret, load_rules(2020))
    # User chose to itemize — the line 10b deduction is unavailable.
    assert r.agi == Decimal("100000.00"), r.agi


def test_ty2021_below_line_charity_reduces_taxable_not_agi():
    ret = Return(
        tax_year=2021,
        filing_status=FilingStatus.MFJ,
        wages=Decimal(100_000),
        charitable_contributions_non_itemizer=Decimal(600),
    )
    r = compute(ret, load_rules(2021))
    # AGI is unchanged at $100,000 (below-the-line item).
    assert r.agi == Decimal("100000.00"), r.agi
    # Taxable = 100,000 − 25,100 (MFJ 2021 std) − 600 charity = 74,300
    assert r.taxable_income == Decimal("74300.00"), r.taxable_income


def test_ty2021_below_line_charity_single_capped_at_300():
    ret = Return(
        tax_year=2021,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(60_000),
        charitable_contributions_non_itemizer=Decimal(800),
    )
    r = compute(ret, load_rules(2021))
    # Single 2021 cap is $300 (vs $600 MFJ).
    # Taxable = 60,000 − 12,550 std − 300 = 47,150
    assert r.taxable_income == Decimal("47150.00"), r.taxable_income


def test_ty2022_field_is_silently_ignored():
    ret = Return(
        tax_year=2022,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(60_000),
        charitable_contributions_non_itemizer=Decimal(300),
    )
    r = compute(ret, load_rules(2022))
    # Provision expired after 2021 — field has no effect.
    assert r.agi == Decimal("60000.00"), r.agi
    assert r.taxable_income == Decimal("60000.00") - Decimal("12950.00"), r.taxable_income


def test_ty2020_real_world_hr_block_reconciles_better():
    """The user's real 2020 import showed a $-630 reconciliation delta. The
    AGI portion of that gap (-$220) was line 10b. After modeling line 10b,
    the AGI now matches the reported value exactly.
    """
    ret = Return(
        tax_year=2020,
        filing_status=FilingStatus.MFJ,
        wages=Decimal(176_865),
        interest_income=Decimal(481),
        ordinary_dividends=Decimal(2_223),
        qualified_dividends=Decimal(1_374),
        long_term_capital_gains=Decimal(16_383),
        short_term_capital_gains=Decimal(20_637),
        unemployment_compensation=Decimal(26_188),
        federal_withholding=Decimal(27_045),
        charitable_contributions_non_itemizer=Decimal(220),
        agi_reported=Decimal(242_557),
    )
    r = compute(ret, load_rules(2020))
    assert r.agi == Decimal("242557.00"), r.agi
