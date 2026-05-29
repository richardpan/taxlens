"""Phase 2 federal credits: dependent care, residential clean energy, clean vehicle."""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def _ret(**kw) -> Return:
    base = dict(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(60_000),
        federal_withholding=Decimal(5_000),
    )
    base.update(kw)
    return Return(**base)


# ── Dependent Care Credit (Form 2441) ────────────────────────────────────────

def test_dcc_zero_when_no_qualifying_person() -> None:
    r = _ret(dependent_care_expenses=Decimal(5_000), num_qualifying_care_persons=0)
    result = compute(r, load_rules(2024))
    assert result.dependent_care_credit == Decimal(0)


def test_dcc_capped_at_3k_for_one_child() -> None:
    """High-AGI single, 1 child, $5k spent → cap = $3k, rate = 20%, credit = $600."""
    r = _ret(
        wages=Decimal(80_000),
        dependent_care_expenses=Decimal(5_000),
        num_qualifying_care_persons=1,
    )
    result = compute(r, load_rules(2024))
    assert result.dependent_care_credit == Decimal("600.00")


def test_dcc_capped_at_6k_for_two_plus() -> None:
    r = _ret(
        wages=Decimal(80_000),
        dependent_care_expenses=Decimal(8_000),
        num_qualifying_care_persons=2,
    )
    result = compute(r, load_rules(2024))
    # cap $6k × 20% = $1200
    assert result.dependent_care_credit == Decimal("1200.00")


def test_dcc_low_income_higher_rate() -> None:
    """AGI ≤ $15k → 35% rate."""
    r = _ret(
        wages=Decimal(14_000),
        dependent_care_expenses=Decimal(3_000),
        num_qualifying_care_persons=1,
    )
    result = compute(r, load_rules(2024))
    # min(3000, 3000, 14000) × 0.35 = 1050
    assert result.dependent_care_credit == Decimal("1050.00")


def test_dcc_mfj_requires_both_spouses_earned() -> None:
    """MFJ with $0 spouse_earned_income → credit disallowed."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(80_000),
        spouse_earned_income=Decimal(0),
        dependent_care_expenses=Decimal(5_000),
        num_qualifying_care_persons=1,
    )
    result = compute(r, load_rules(2024))
    assert result.dependent_care_credit == Decimal(0)


def test_dcc_mfj_limited_by_lesser_spouse_earned() -> None:
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(100_000),
        spouse_earned_income=Decimal(2_000),
        dependent_care_expenses=Decimal(5_000),
        num_qualifying_care_persons=2,
    )
    result = compute(r, load_rules(2024))
    # min(5000, 6000, 2000) = 2000; AGI=102k → 20% rate; credit = $400
    assert result.dependent_care_credit == Decimal("400.00")


def test_dcc_arpa_2021_refundable_and_higher_cap() -> None:
    """TY2021 ARPA: cap $8k/$16k, up to 50%, refundable."""
    r = _ret(
        tax_year=2021,
        wages=Decimal(60_000),
        dependent_care_expenses=Decimal(10_000),
        num_qualifying_care_persons=1,
    )
    result = compute(r, load_rules(2021))
    # AGI=60k → 50% rate (still in flat zone, AGI < 125k); cap $8k → credit $4000
    # Refundable in 2021
    assert result.dependent_care_credit == Decimal(0)
    assert result.dependent_care_credit_refundable == Decimal("4000.00")


# ── Residential Clean Energy Credit (Form 5695) ──────────────────────────────

def test_rce_30pct_in_2024() -> None:
    r = _ret(residential_clean_energy_cost=Decimal(20_000))
    result = compute(r, load_rules(2024))
    assert result.residential_clean_energy_credit == Decimal("6000.00")


def test_rce_26pct_in_2021() -> None:
    """Rate dropped to 26% in 2020-2021 before IRA restored to 30%."""
    r = _ret(tax_year=2021, residential_clean_energy_cost=Decimal(20_000))
    result = compute(r, load_rules(2021))
    assert result.residential_clean_energy_credit == Decimal("5200.00")


def test_rce_zero_when_no_cost() -> None:
    r = _ret()
    result = compute(r, load_rules(2024))
    assert result.residential_clean_energy_credit == Decimal(0)


# ── Clean Vehicle Credit (Form 8936) ─────────────────────────────────────────

def test_cvc_pre_2023_no_magi_cap() -> None:
    """Before 2023 there's no AGI-based disqualification."""
    r = _ret(
        tax_year=2022,
        wages=Decimal(500_000),
        clean_vehicle_credit_claimed=Decimal(7_500),
    )
    result = compute(r, load_rules(2022))
    assert result.clean_vehicle_credit == Decimal("7500.00")


def test_cvc_2024_disqualified_above_single_cap() -> None:
    """Single 2024: MAGI cap is $150k. AGI $200k → fully disqualified."""
    r = _ret(
        wages=Decimal(200_000),
        clean_vehicle_credit_claimed=Decimal(7_500),
    )
    result = compute(r, load_rules(2024))
    assert result.clean_vehicle_credit == Decimal(0)


def test_cvc_2024_allowed_below_single_cap() -> None:
    r = _ret(
        wages=Decimal(120_000),
        clean_vehicle_credit_claimed=Decimal(7_500),
    )
    result = compute(r, load_rules(2024))
    assert result.clean_vehicle_credit == Decimal("7500.00")


def test_cvc_used_vehicle_lower_cap() -> None:
    """Used vehicle: single MAGI cap is $75k. AGI $100k → disqualified."""
    r = _ret(
        wages=Decimal(100_000),
        clean_vehicle_credit_claimed=Decimal(4_000),
        clean_vehicle_is_used=True,
    )
    result = compute(r, load_rules(2024))
    assert result.clean_vehicle_credit == Decimal(0)


def test_cvc_mfj_higher_cap() -> None:
    """MFJ 2024: new vehicle MAGI cap $300k."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(250_000),
        clean_vehicle_credit_claimed=Decimal(7_500),
    )
    result = compute(r, load_rules(2024))
    assert result.clean_vehicle_credit == Decimal("7500.00")


# ── Integration: all three credits applied together ──────────────────────────

def test_all_three_credits_reduce_total_tax() -> None:
    r = _ret(
        wages=Decimal(100_000),
        dependent_care_expenses=Decimal(3_000),
        num_qualifying_care_persons=1,
        residential_clean_energy_cost=Decimal(10_000),
        clean_vehicle_credit_claimed=Decimal(7_500),
    )
    result = compute(r, load_rules(2024))
    # DCC: 3000 × 20% = 600
    # RCE: 10000 × 0.30 = 3000
    # CVC: 7500 (under 150k MAGI cap)
    assert result.dependent_care_credit == Decimal("600.00")
    assert result.residential_clean_energy_credit == Decimal("3000.00")
    assert result.clean_vehicle_credit == Decimal("7500.00")
    # Combined credits flow into the credits aggregate
    assert result.credits >= Decimal("11100.00")
