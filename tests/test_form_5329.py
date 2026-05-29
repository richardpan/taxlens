"""Form 5329 — §4973 excess IRA contribution excise + §4974 RMD shortfall excise."""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def _ret(**kw) -> Return:
    base = dict(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(50_000),
        federal_withholding=Decimal(0),
    )
    base.update(kw)
    return Return(**base)


# ── §4973 excess contribution excise ────────────────────────────────────────

def test_within_limit_no_excise() -> None:
    r = _ret(roth_ira_contributions=Decimal(7_000))  # 2024 cap
    result = compute(r, load_rules(2024))
    assert result.excess_ira_contribution_excise == Decimal(0)
    assert result.excess_ira_contributions_out == Decimal(0)


def test_combined_over_cap_triggers_6pct() -> None:
    # Trad 4k + Roth 5k = 9k > 7k cap → 2k excess × 6% = $120
    r = _ret(
        traditional_ira_contributions=Decimal(4_000),
        roth_ira_contributions=Decimal(5_000),
    )
    result = compute(r, load_rules(2024))
    assert result.excess_ira_contribution_excise == Decimal("120.00")
    assert result.excess_ira_contributions_out == Decimal("2000.00")


def test_roth_phased_out_at_high_magi() -> None:
    # Single, AGI $200k >> phaseout end ($161k). Roth $7k all excess. 6% = $420.
    r = _ret(wages=Decimal(200_000), roth_ira_contributions=Decimal(7_000))
    result = compute(r, load_rules(2024))
    assert result.excess_ira_contribution_excise == Decimal("420.00")
    assert result.excess_ira_contributions_out == Decimal("7000.00")


def test_excess_carryforward_excised_again_until_removed() -> None:
    # $3k carry-in, no new contribs, no removal → 6% × $3k = $180.
    r = _ret(excess_ira_contributions_in=Decimal(3_000))
    result = compute(r, load_rules(2024))
    assert result.excess_ira_contribution_excise == Decimal("180.00")
    assert result.excess_ira_contributions_out == Decimal("3000.00")


def test_corrective_distribution_clears_excise() -> None:
    r = _ret(
        excess_ira_contributions_in=Decimal(3_000),
        excess_ira_contributions_removed=Decimal(3_000),
    )
    result = compute(r, load_rules(2024))
    assert result.excess_ira_contribution_excise == Decimal(0)
    assert result.excess_ira_contributions_out == Decimal(0)


# ── §4974 RMD shortfall excise ──────────────────────────────────────────────

def test_rmd_fully_satisfied_no_excise() -> None:
    r = _ret(
        required_minimum_distribution=Decimal(10_000),
        ira_distributions_taxable=Decimal(12_000),
    )
    result = compute(r, load_rules(2024))
    assert result.rmd_shortfall == Decimal(0)
    assert result.rmd_shortfall_excise == Decimal(0)


def test_rmd_shortfall_2024_at_25pct() -> None:
    # SECURE 2.0 rate for 2023+ = 25%. Shortfall $4k → $1,000 excise.
    r = _ret(
        required_minimum_distribution=Decimal(10_000),
        ira_distributions_taxable=Decimal(6_000),
    )
    result = compute(r, load_rules(2024))
    assert result.rmd_shortfall == Decimal("4000.00")
    assert result.rmd_shortfall_excise == Decimal("1000.00")


def test_rmd_shortfall_pre_secure2_at_50pct() -> None:
    # 2022 (pre-SECURE 2.0): rate is the default 50%. Shortfall $4k → $2,000.
    r = _ret(
        tax_year=2022,
        required_minimum_distribution=Decimal(10_000),
        ira_distributions_taxable=Decimal(6_000),
    )
    result = compute(r, load_rules(2022))
    assert result.rmd_shortfall == Decimal("4000.00")
    assert result.rmd_shortfall_excise == Decimal("2000.00")


def test_rmd_counts_pension_distributions_too() -> None:
    r = _ret(
        required_minimum_distribution=Decimal(10_000),
        ira_distributions_taxable=Decimal(3_000),
        pension_distributions_taxable=Decimal(7_000),
    )
    result = compute(r, load_rules(2024))
    assert result.rmd_shortfall == Decimal(0)
