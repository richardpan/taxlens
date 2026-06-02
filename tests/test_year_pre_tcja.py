"""Smoke tests for TY 2010 - 2014 federal rules (pre-TCJA, pre-ATRA-era).

These years pre-date many post-2013 features (NIIT, Additional Medicare
Tax, the 39.6% bracket, Pease, PEP), so the tests verify both that the
YAML loads with the right shape and that the engine walks the brackets
correctly with the personal exemption subtracted.

End-to-end expectations were computed by hand against the IRS Rev. Proc.
bracket schedules (sources cited in each YAML).
"""
from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


# --- Spot-checks on a few rule-load values across the period. ---------

def test_2010_rules_load() -> None:
    rules = load_rules(2010)
    assert rules.year == 2010
    assert rules.standard_deduction["single"] == 5700
    assert rules.standard_deduction["mfj"] == 11400
    assert rules.personal_exemption["amount"] == 3650
    # Pre-ACA: NIIT and Additional Medicare did not exist; we stub them
    # at rate 0 so the engine still loads.
    assert rules.niit["rate"] == 0
    assert rules.additional_medicare["rate"] == 0
    # No PEP/Pease in 2010 (suspended through 2012).
    assert rules.pease is None
    assert "phaseout_start" not in rules.personal_exemption
    # Top bracket is 35%, not 39.6%.
    assert rules.ordinary_brackets["single"][-1][1] == Decimal("0.35")


def test_2011_payroll_tax_holiday_reflected_in_se_rate() -> None:
    """TRA-2010 cut the employee SS payroll rate to 4.2% for 2011, so SE
    rate is 10.4% (vs. the usual 12.4%) for that year only."""
    rules = load_rules(2011)
    assert rules.se_tax["social_security_rate"] == Decimal("0.104")
    assert rules.se_tax["social_security_wage_base"] == 106800


def test_2012_amt_patch_values() -> None:
    rules = load_rules(2012)
    # ATRA-2012 retroactive AMT patch — these are the permanent values.
    assert rules.amt["exemption"]["single"] == 50600
    assert rules.amt["exemption"]["mfj"] == 78750


def test_2013_introduces_top_bracket_niit_pease() -> None:
    rules = load_rules(2013)
    # First year of the 39.6% top bracket.
    assert rules.ordinary_brackets["single"][-1] == (Decimal(400000), Decimal("0.396"))
    # First year of NIIT and Additional Medicare Tax.
    assert rules.niit["rate"] == Decimal("0.038")
    assert rules.additional_medicare["rate"] == Decimal("0.009")
    # PEP and Pease reinstated.
    assert rules.pease is not None
    assert rules.pease["threshold"]["single"] == 250000
    assert rules.personal_exemption["phaseout_start"]["mfj"] == 300000


def test_2014_inflation_bumps() -> None:
    rules = load_rules(2014)
    assert rules.standard_deduction["single"] == 6200
    assert rules.standard_deduction["mfj"] == 12400
    assert rules.personal_exemption["amount"] == 3950
    assert rules.se_tax["social_security_wage_base"] == 117000


# --- End-to-end bracket walks for a $100k single filer. ---------------

# Each entry: (year, deduction+exemption, taxable_income, expected_ordinary_tax).
# Ordinary tax computed by walking the IRS bracket schedule by hand.
_SINGLE_100K_CASES = [
    (2010, Decimal("9350.00"),  Decimal("90650.00"), Decimal("19091.25")),
    (2011, Decimal("9500.00"),  Decimal("90500.00"), Decimal("18957.00")),
    (2012, Decimal("9750.00"),  Decimal("90250.00"), Decimal("18730.50")),
    (2013, Decimal("10000.00"), Decimal("90000.00"), Decimal("18493.25")),
    (2014, Decimal("10150.00"), Decimal("89850.00"), Decimal("18333.75")),
]


@pytest.mark.parametrize("year,deduction_total,taxable,expected_tax", _SINGLE_100K_CASES)
def test_pre_tcja_single_100k_walks_brackets_correctly(
    year: int,
    deduction_total: Decimal,
    taxable: Decimal,
    expected_tax: Decimal,
) -> None:
    """Single filer, $100k wages, no other income. Confirm AGI, the
    combined std-deduction + personal-exemption subtraction, and the
    final ordinary tax all line up with the IRS bracket schedule."""
    r = Return(
        tax_year=year,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(100_000),
        federal_withholding=Decimal(0),
    )
    result = compute(r, load_rules(year))
    assert result.agi == Decimal("100000.00")
    # deduction_used + personal_exemption_used should equal our hand
    # computation for the combined subtraction.
    combined = result.deduction_used + result.personal_exemption_used
    assert combined == deduction_total
    assert result.taxable_income == taxable
    assert result.ordinary_tax == expected_tax
