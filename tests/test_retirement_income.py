"""Phase 2 federal coverage: retirement income (1099-R + SSA-1099)."""
from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def _ret(**kw) -> Return:
    base = dict(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(0),
        federal_withholding=Decimal(0),
    )
    base.update(kw)
    return Return(**base)


# ── Social Security §86 ──────────────────────────────────────────────────────

def test_ss_not_taxed_when_provisional_income_below_base() -> None:
    r = _ret(
        social_security_benefits=Decimal(20_000),
        # PI = 0 wages + 0 exempt + 10k (½ SS) = 10k < 25k base
    )
    result = compute(r, load_rules(2024))
    assert result.social_security_taxable == Decimal(0)
    # No SS in AGI either
    assert result.agi == Decimal(0)


def test_ss_50pct_taxable_in_first_tier() -> None:
    """Single, $30k wages + $20k SS → PI = 30k + 10k = 40k.
    Base 25k, second 34k. PI is in second tier (above 34k), not first."""
    r = _ret(
        wages=Decimal(20_000),
        social_security_benefits=Decimal(20_000),
        # PI = 20k + 10k = 30k → in first tier (25k < PI <= 34k)
    )
    result = compute(r, load_rules(2024))
    # First-tier formula: min(SS × 0.5, (PI - base) × 0.5) = min(10k, 2.5k) = 2.5k
    assert result.social_security_taxable == Decimal("2500.00")


def test_ss_85pct_taxable_in_second_tier() -> None:
    """Single, high income → PI well above $34k → 85% taxable, capped at 85% × SS."""
    r = _ret(
        wages=Decimal(80_000),
        social_security_benefits=Decimal(20_000),
        # PI = 80k + 10k = 90k → above 34k second threshold
    )
    result = compute(r, load_rules(2024))
    # Second-tier formula: min(SS × 0.85, tier1_max + (PI - second) × 0.85)
    #   tier1_max = (34k - 25k) × 0.5 = 4500
    #   formula  = 4500 + (90k - 34k) × 0.85 = 4500 + 47600 = 52100
    #   capped at 20k × 0.85 = 17000
    assert result.social_security_taxable == Decimal("17000.00")


def test_ss_taxability_includes_tax_exempt_interest_in_provisional() -> None:
    """§86 includes muni-bond interest in PI even though it's not in AGI."""
    r = _ret(
        wages=Decimal(20_000),
        tax_exempt_interest=Decimal(8_000),
        social_security_benefits=Decimal(20_000),
        # PI = 20k + 8k exempt + 10k (½ SS) = 38k → second tier
    )
    result = compute(r, load_rules(2024))
    # Should be > the 2500 the same wages without exempt interest would yield.
    assert result.social_security_taxable > Decimal(2500)


def test_mfj_higher_ss_thresholds() -> None:
    """MFJ base/second thresholds are higher (32k/44k vs 25k/34k single)."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(25_000),
        social_security_benefits=Decimal(20_000),
        # PI = 25k + 10k = 35k → above MFJ base 32k but below second 44k → tier 1
    )
    result = compute(r, load_rules(2024))
    # min(20k × 0.5, (35k - 32k) × 0.5) = min(10k, 1.5k) = 1500
    assert result.social_security_taxable == Decimal("1500.00")


# ── Pension + IRA distributions ──────────────────────────────────────────────

def test_pension_distributions_flow_into_agi() -> None:
    r = _ret(
        pension_distributions_taxable=Decimal(30_000),
    )
    result = compute(r, load_rules(2024))
    assert result.agi == Decimal(30_000)
    assert result.pension_taxable == Decimal("30000.00")


def test_ira_distributions_flow_into_agi() -> None:
    r = _ret(
        ira_distributions_taxable=Decimal(15_000),
    )
    result = compute(r, load_rules(2024))
    assert result.agi == Decimal(15_000)
    assert result.ira_taxable == Decimal("15000.00")


def test_mixed_retirement_income_full_round_trip() -> None:
    """Realistic retiree: small wages + pension + IRA + SS."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(10_000),
        pension_distributions_taxable=Decimal(40_000),
        ira_distributions_taxable=Decimal(15_000),
        social_security_benefits=Decimal(30_000),
        # gross excl SS = 10k + 40k + 15k = 65k
        # PI = 65k + 15k (½ SS) = 80k → second tier (above MFJ second 44k)
        # tier1_max = (44k - 32k) × 0.5 = 6000
        # taxable_ss = min(30k × 0.85, 6000 + (80k - 44k) × 0.85)
        #            = min(25500, 36600) = 25500
        # AGI = 65k + 25500 = 90500
    )
    result = compute(r, load_rules(2024))
    assert result.social_security_taxable == Decimal("25500.00")
    assert result.agi == Decimal("90500.00")
    assert result.total_tax > 0


# ── Early withdrawal penalty (§72(t)) ────────────────────────────────────────

def test_early_withdrawal_penalty_adds_10pct() -> None:
    r = _ret(
        wages=Decimal(60_000),
        ira_distributions_taxable=Decimal(20_000),
        early_withdrawal_subject_to_penalty=Decimal(20_000),
    )
    result = compute(r, load_rules(2024))
    # 10% × $20k = $2000 added to total tax
    assert result.early_withdrawal_penalty == Decimal("2000.00")


def test_no_early_withdrawal_penalty_when_zero() -> None:
    r = _ret(
        wages=Decimal(60_000),
        ira_distributions_taxable=Decimal(20_000),
        # early_withdrawal_subject_to_penalty defaults to 0
    )
    result = compute(r, load_rules(2024))
    assert result.early_withdrawal_penalty == Decimal(0)
