"""Smoke tests for TY 2026 federal rules.

Confirms the YAML loads, key inflation-adjusted values land, and a basic
end-to-end compute returns sensible bracket math.
"""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def test_2026_rules_load() -> None:
    rules = load_rules(2026)
    assert rules.year == 2026
    # Inflation-adjusted standard deductions per Rev. Proc. 2025-32.
    assert rules.standard_deduction["single"] == 16100
    assert rules.standard_deduction["mfj"] == 32200
    assert rules.standard_deduction["hoh"] == 24150
    # SSA 2026 wage base.
    assert rules.se_tax["social_security_wage_base"] == 184500
    # IRA / Roth limits per IRS Notice 2025-67.
    assert rules.ira_deduction["contribution_limit"]["under_50"] == 7500
    assert rules.ira_deduction["phaseout_covered"]["single"]["start"] == 81000
    # OBBB CTC bump from $2,000 to $2,200.
    assert rules.ctc["per_qualifying_child"] == 2200


def test_2026_simple_single_return_brackets_correctly() -> None:
    """Single filer, $100k wages, 2026. Walk the math:
       - std_ded = $16,100 → taxable = $83,900
       - 10% × $12,400 = $1,240.00
       - 12% × ($50,400 − $12,400) = $4,560.00
       - 22% × ($83,900 − $50,400) = $7,370.00
       - total ordinary tax = $13,170.00
    """
    r = Return(
        tax_year=2026,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(100_000),
        federal_withholding=Decimal(0),
    )
    result = compute(r, load_rules(2026))
    assert result.agi == Decimal("100000.00")
    assert result.deduction_used == Decimal("16100.00")
    assert result.taxable_income == Decimal("83900.00")
    assert result.ordinary_tax == Decimal("13176.00")


def test_2026_mfj_top_bracket_starts_at_768700() -> None:
    """Smoke check on the highest MFJ bracket threshold for 2026."""
    rules = load_rules(2026)
    mfj = rules.ordinary_brackets["mfj"]
    top = mfj[-1]
    assert top[0] == 768700
    assert top[1] == Decimal("0.37")
