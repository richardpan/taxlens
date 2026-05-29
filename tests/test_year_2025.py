"""Smoke tests for TY 2025 federal rules.

Confirms the YAML loads, key inflation-adjusted values land, and a basic
end-to-end compute returns sensible bracket math.
"""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def test_2025_rules_load() -> None:
    rules = load_rules(2025)
    assert rules.year == 2025
    # Inflation-adjusted standard deductions per Rev. Proc. 2024-40.
    assert rules.standard_deduction["single"] == 15000
    assert rules.standard_deduction["mfj"] == 30000
    assert rules.standard_deduction["hoh"] == 22500
    # SSA 2025 wage base.
    assert rules.se_tax["social_security_wage_base"] == 176100
    # IRA limits + new active-participant phaseout windows.
    assert rules.ira_deduction["contribution_limit"]["under_50"] == 7000
    assert rules.ira_deduction["phaseout_covered"]["single"]["start"] == 79000


def test_2025_simple_single_return_brackets_correctly() -> None:
    """Single filer, $100k wages, 2025. Walk the math:
       - std_ded = $15,000 → taxable = $85,000
       - 10% × $11,925 = $1,192.50
       - 12% × ($48,475 − $11,925) = $4,386.00
       - 22% × ($85,000 − $48,475) = $8,035.50
       - total ordinary tax = $13,614.00
    """
    r = Return(
        tax_year=2025,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(100_000),
        federal_withholding=Decimal(0),
    )
    result = compute(r, load_rules(2025))
    assert result.agi == Decimal("100000.00")
    assert result.deduction_used == Decimal("15000.00")
    assert result.taxable_income == Decimal("85000.00")
    assert result.ordinary_tax == Decimal("13614.00")


def test_2025_mfj_top_bracket_starts_at_751600() -> None:
    """Smoke check on the highest MFJ bracket threshold for 2025."""
    rules = load_rules(2025)
    mfj = rules.ordinary_brackets["mfj"]
    top = mfj[-1]
    assert top[0] == 751600
    assert top[1] == Decimal("0.37")
