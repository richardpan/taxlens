"""Tests for v0.41 simulator enhancements: multi-year Roth ladder,
IRMAA tier detection, and TLH carryforward projection.
"""
from decimal import Decimal

from taxlens.models import FilingStatus, Return
from taxlens.simulators import (
    _irmaa_lookup,
    project_tlh_carryforward,
    simulate_roth_conversion,
    simulate_roth_conversion_ladder,
    simulate_tax_loss_harvest,
)


def _retiree(year: int = 2024, status: FilingStatus = FilingStatus.MFJ) -> Return:
    """A typical pre-Medicare retiree: low W-2, some dividends, no SS yet.
    Sets up a clean test bed for Roth ladder scenarios."""
    return Return(
        tax_year=year,
        filing_status=status,
        wages=Decimal("0"),
        interest_income=Decimal("8000"),
        ordinary_dividends=Decimal("12000"),
        qualified_dividends=Decimal("10000"),
    )


# ── IRMAA lookup ───────────────────────────────────────────────────────

def test_irmaa_below_threshold_tier_zero() -> None:
    tier, surcharge = _irmaa_lookup(Decimal("90000"), 2025, "single")
    assert tier == 0
    assert surcharge == Decimal("0")


def test_irmaa_mfj_just_over_first_threshold() -> None:
    """MFJ first IRMAA threshold for 2025 is $212k; landing at $215k
    moves to tier 1 ($74/mo Part B surcharge)."""
    tier, surcharge = _irmaa_lookup(Decimal("215000"), 2025, "mfj")
    assert tier == 1
    assert surcharge == Decimal("74.00")


def test_irmaa_top_tier_uncapped() -> None:
    tier, surcharge = _irmaa_lookup(Decimal("2000000"), 2025, "mfj")
    assert surcharge == Decimal("443.90")
    assert tier >= 5


# ── Roth conversion: IRMAA notes attached when tier rises ─────────────

def test_roth_conversion_attaches_irmaa_note_when_crossing_tier() -> None:
    """A retiree with $20k of investment income converts $250k of
    traditional IRA → Roth. AGI shoots from ~$20k to ~$270k MFJ,
    crossing the $266k tier-2 threshold."""
    base = _retiree(year=2024, status=FilingStatus.MFJ)
    sim = simulate_roth_conversion(base, Decimal("250000"))
    assert sim.tax_delta > 0
    assert any("IRMAA" in n for n in sim.notes), sim.notes


def test_roth_conversion_no_irmaa_note_when_tier_unchanged() -> None:
    """A small conversion that keeps MFJ MAGI below $212k → no IRMAA flag."""
    base = _retiree(year=2024, status=FilingStatus.MFJ)
    sim = simulate_roth_conversion(base, Decimal("20000"))
    assert sim.notes == [] or all("IRMAA" not in n for n in sim.notes)


# ── Multi-year Roth ladder ─────────────────────────────────────────────

def test_roth_ladder_projects_three_years() -> None:
    base = _retiree(year=2024, status=FilingStatus.MFJ)
    schedule = [Decimal("40000"), Decimal("40000"), Decimal("40000")]
    ladder = simulate_roth_conversion_ladder(base, schedule)
    assert len(ladder.rungs) == 3
    assert [r.year for r in ladder.rungs] == [2024, 2025, 2026]
    assert ladder.cumulative_converted == Decimal("120000")
    # Each rung incurs some tax (small, because of std deduction + $20k of
    # baseline qualified income, but conversions push past those shields).
    assert ladder.cumulative_tax > 0
    # Avg marginal rate should be reasonable for a low-income ladder —
    # mostly 10% bracket on $40k/yr conversions over a ~$30k std deduction.
    # Empirically lands ~5% (qualified income gets shoved into the 15% LTCG
    # band as ordinary fills more of the bottom bracket).
    assert Decimal("0.02") <= ladder.avg_marginal_rate <= Decimal("0.30")


def test_roth_ladder_zero_amount_rung_is_pass_through() -> None:
    """A $0 rung should produce zero tax delta (the engine sees the same
    return as the no-conversion baseline)."""
    base = _retiree(year=2024)
    ladder = simulate_roth_conversion_ladder(
        base, [Decimal("30000"), Decimal("0"), Decimal("30000")],
    )
    assert ladder.rungs[1].amount == Decimal("0")
    assert ladder.rungs[1].tax_delta == Decimal("0")
    assert ladder.rungs[1].marginal_rate == Decimal("0")


def test_roth_ladder_rejects_negative_rung() -> None:
    base = _retiree(year=2024)
    try:
        simulate_roth_conversion_ladder(base, [Decimal("-1000")])
    except ValueError as e:
        assert "negative" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_roth_ladder_rejects_empty_schedule() -> None:
    base = _retiree(year=2024)
    try:
        simulate_roth_conversion_ladder(base, [])
    except ValueError as e:
        assert "non-empty" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ── TLH wash-sale note + carryforward projection ─────────────────────

def test_tlh_attaches_wash_sale_note() -> None:
    base = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal("180000"),
        long_term_capital_gains=Decimal("0"),
    )
    sim = simulate_tax_loss_harvest(base, Decimal("20000"))
    assert any("wash-sale" in n.lower() or "§1091" in n for n in sim.notes)


def test_tlh_carryforward_projection_3k_per_year() -> None:
    """A $20k loss with no offsetting gains depletes via $3k/year ordinary
    offset over ~7 years. Final year burns the residual $2k."""
    proj = project_tlh_carryforward(Decimal("20000"), start_year=2024, years=10)
    assert len(proj) == 7
    assert proj[0].year == 2024
    assert proj[0].carryforward_in == Decimal("20000")
    assert proj[0].used_against_ordinary == Decimal("3000")
    assert proj[0].carryforward_out == Decimal("17000")
    assert proj[6].carryforward_out == Decimal("0")
    assert proj[6].used_against_ordinary == Decimal("2000")


def test_tlh_carryforward_projection_empty_when_no_loss() -> None:
    assert project_tlh_carryforward(Decimal("0"), 2024) == []
    assert project_tlh_carryforward(Decimal("-5000"), 2024) == []


def test_tlh_carryforward_projection_clamps_to_year_window() -> None:
    """A $50k loss across only 3 years projects 3 partial-depletion rows
    (we still have $41k carrying past the window)."""
    proj = project_tlh_carryforward(Decimal("50000"), start_year=2024, years=3)
    assert len(proj) == 3
    assert proj[-1].carryforward_out == Decimal("41000")
