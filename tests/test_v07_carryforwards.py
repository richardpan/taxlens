"""Tests for v0.7: NOL, AMT credit, FTC, charitable carryover, MD county piggyback."""
from __future__ import annotations

from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


# ---------- NOL §172 -----------------------------------------------------

def test_nol_offsets_80pct_of_taxable():
    """$100k NOL_in against $50k taxable → 80% × 50k = $40k used, $60k carries out."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                  wages=Decimal("64600"),  # ~$50k taxable after $14,600 std ded
                  nol_carryforward_in=Decimal("100000"))
    res = compute(ret)
    assert res.nol_carryforward_out > 0
    # Should be roughly 100k - 0.8 * 50k = 60k
    assert Decimal("55000") <= res.nol_carryforward_out <= Decimal("65000")


def test_nol_fully_used_when_small():
    """Small NOL under 80% cap → fully used, nothing carries."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                  wages=Decimal("114600"),  # ~$100k taxable
                  nol_carryforward_in=Decimal("5000"))
    res = compute(ret)
    assert res.nol_carryforward_out == Decimal("0")


# ---------- Foreign Tax Credit -------------------------------------------

def test_ftc_offsets_regular_tax():
    base = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                   wages=Decimal("100000"))
    with_ftc = base.model_copy(update={"foreign_taxes_paid": Decimal("2000")})
    r0 = compute(base)
    r1 = compute(with_ftc)
    assert r0.total_tax - r1.total_tax == Decimal("2000")
    assert r1.ftc_carryforward_out == Decimal("0")


def test_ftc_excess_carries_forward():
    """Massive foreign tax > regular tax → excess carries forward."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                  wages=Decimal("50000"),
                  foreign_taxes_paid=Decimal("100000"))
    res = compute(ret)
    assert res.ftc_carryforward_out > 0


# ---------- AMT credit (Form 8801) ---------------------------------------

def test_amt_credit_used_when_no_amt_this_year():
    """Prior AMT credit usable in a year with no AMT and positive regular tax."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                  wages=Decimal("100000"),
                  amt_credit_carryforward_in=Decimal("3000"))
    base = ret.model_copy(update={"amt_credit_carryforward_in": Decimal("0")})
    r0 = compute(base)
    r1 = compute(ret)
    assert r0.total_tax - r1.total_tax == Decimal("3000")


def test_amt_this_year_grows_carryforward():
    """An ISO exercise generates AMT this year; that AMT becomes future credit."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                  wages=Decimal("200000"),
                  iso_bargain_element=Decimal("300000"),
                  amt_adjustments=Decimal("300000"))
    res = compute(ret)
    assert res.amt > 0
    assert res.amt_credit_carryforward_out >= res.amt


# ---------- Charitable §170(d) carryover ---------------------------------

def test_charitable_carryover_used_when_itemizing():
    """Prior-year carry-in adds to itemized deductions next year."""
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                  wages=Decimal("200000"),
                  itemized_deductions=Decimal("30000"),
                  charitable_contributions=Decimal("10000"),
                  charitable_carryover_in=Decimal("5000"))
    base = ret.model_copy(update={"charitable_carryover_in": Decimal("0")})
    r0 = compute(base)
    r1 = compute(ret)
    # The carryover should reduce taxable income (and thus tax).
    assert r1.total_tax < r0.total_tax


# ---------- MD county piggyback ------------------------------------------

@pytest.mark.parametrize("locality,rate", [
    ("MD_MONTGOMERY", Decimal("0.0320")),
    ("MD_ANNE_ARUNDEL", Decimal("0.0270")),
])
def test_md_county_piggyback(locality, rate):
    base = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                   state="MD", wages=Decimal("100000"))
    r_no = compute(base)
    r_yes = compute(base.model_copy(update={"locality": locality}))
    assert r_yes.state_result.locality == locality
    # Locality tax = rate × MD taxable income (~ $97,450 single).
    expected = (r_no.state_result.state_taxable_income * rate).quantize(Decimal("1"))
    assert abs(r_yes.state_result.locality_tax - expected) <= Decimal("2.00")
