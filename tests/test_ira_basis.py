"""Form 8606 — nondeductible Traditional IRA basis tracking + §72(b) pro-rata
basis recovery on distributions."""
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


# ── No basis, no distribution ────────────────────────────────────────────────

def test_no_basis_no_distribution_emits_zeros() -> None:
    r = _ret()
    result = compute(r, load_rules(2024))
    assert result.ira_basis_out == Decimal(0)
    assert result.ira_distribution_nontaxable == Decimal(0)
    assert result.ira_taxable_after_basis == Decimal(0)


# ── New nondeductible contribution becomes basis ────────────────────────────

def test_disallowed_contribution_carries_as_basis() -> None:
    # MFJ where spouse-only is covered; AGI well above phaseout → fully phased out.
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(300_000),
        spouse_covered_by_workplace_plan=True,
        traditional_ira_contributions=Decimal(7_000),
    )
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal(0)
    assert result.ira_deduction_disallowed == Decimal("7000.00")
    # Basis_out picks up the new nondeductible contribution.
    assert result.ira_basis_out == Decimal("7000.00")


# ── Pro-rata basis recovery on distribution (Form 8606 lines 6-13) ──────────

def test_pro_rata_basis_recovery_partial() -> None:
    # Scenario: $10k carry-in basis, $90k year-end IRA value, $10k distribution.
    # line 8 = 90k + 10k = 100k
    # fraction = 10k / 100k = 0.10
    # nontaxable = 10k × 0.10 = $1,000
    # taxable = 10k − 1k = $9,000
    # basis_remaining = 10k − 1k = $9,000
    r = _ret(
        ira_basis_in=Decimal(10_000),
        ira_year_end_value=Decimal(90_000),
        ira_distributions_taxable=Decimal(10_000),
    )
    result = compute(r, load_rules(2024))
    assert result.ira_distribution_nontaxable == Decimal("1000.00")
    assert result.ira_taxable_after_basis == Decimal("9000.00")
    assert result.ira_basis_out == Decimal("9000.00")
    # AGI should reflect the basis-adjusted (smaller) taxable amount.
    # Wages 50k + IRA taxable 9k = 59k AGI (no adjustments).
    assert result.agi == Decimal("59000.00")


def test_pro_rata_basis_full_liquidation() -> None:
    # Year-end value = 0 → full liquidation; basis recovered up to distribution.
    r = _ret(
        ira_basis_in=Decimal(8_000),
        ira_year_end_value=Decimal(0),
        ira_distributions_taxable=Decimal(20_000),
    )
    result = compute(r, load_rules(2024))
    assert result.ira_distribution_nontaxable == Decimal("8000.00")
    assert result.ira_taxable_after_basis == Decimal("12000.00")
    assert result.ira_basis_out == Decimal(0)


def test_pro_rata_basis_capped_by_distribution() -> None:
    # Basis larger than distribution under full-liquidation: recover only up to dist.
    r = _ret(
        ira_basis_in=Decimal(20_000),
        ira_year_end_value=Decimal(0),
        ira_distributions_taxable=Decimal(5_000),
    )
    result = compute(r, load_rules(2024))
    assert result.ira_distribution_nontaxable == Decimal("5000.00")
    assert result.ira_taxable_after_basis == Decimal(0)
    assert result.ira_basis_out == Decimal("15000.00")


def test_no_basis_means_distribution_fully_taxable() -> None:
    r = _ret(
        ira_basis_in=Decimal(0),
        ira_year_end_value=Decimal(100_000),
        ira_distributions_taxable=Decimal(10_000),
    )
    result = compute(r, load_rules(2024))
    assert result.ira_distribution_nontaxable == Decimal(0)
    assert result.ira_taxable_after_basis == Decimal("10000.00")
    assert result.ira_basis_out == Decimal(0)


# ── Distribution + new nondeductible contribution in same year ──────────────

def test_basis_out_combines_recovered_and_new_contribution() -> None:
    # Carry-in basis 5k, distribute 5k against 45k year-end value (recover
    # 5k × 5/(45+5) = 500), AND contribute 7k that is fully phased out.
    # basis_after_distrib = 5k − 500 = 4500
    # basis_out = 4500 + 7000 = 11500
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(300_000),
        spouse_covered_by_workplace_plan=True,
        traditional_ira_contributions=Decimal(7_000),
        ira_basis_in=Decimal(5_000),
        ira_year_end_value=Decimal(45_000),
        ira_distributions_taxable=Decimal(5_000),
    )
    result = compute(r, load_rules(2024))
    assert result.ira_distribution_nontaxable == Decimal("500.00")
    assert result.ira_taxable_after_basis == Decimal("4500.00")
    assert result.ira_deduction_disallowed == Decimal("7000.00")
    assert result.ira_basis_out == Decimal("11500.00")
