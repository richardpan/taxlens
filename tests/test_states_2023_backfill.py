"""TY 2023 state backfills (PA, OH, NC, AZ, MN) — load and compute checks.

Each new YAML must:
  - parse cleanly via load_state_rules
  - produce a non-negative state_tax under a representative wage scenario
  - match a hand-computed bracket-walk for at least one filing status
"""
from decimal import Decimal, ROUND_HALF_UP

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_state_rules


def _ret(state: str, wages: Decimal, status: FilingStatus = FilingStatus.SINGLE) -> Return:
    return Return(tax_year=2023, filing_status=status, wages=wages, state=state)


def _q(d: Decimal) -> Decimal:
    """Match the engine's whole-cent rounding (ROUND_HALF_UP)."""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@pytest.mark.parametrize("state", ["PA", "OH", "NC", "AZ", "MN"])
def test_2023_backfill_loads(state: str) -> None:
    rules = load_state_rules(state, 2023)
    assert rules.state == state
    assert rules.year == 2023
    # Every filing status must be represented.
    for fs in ("single", "mfj", "mfs", "hoh", "qss"):
        assert fs in rules.standard_deduction
        assert fs in rules.ordinary_brackets


@pytest.mark.parametrize("state", ["PA", "OH", "NC", "AZ", "MN"])
def test_2023_backfill_computes_nonneg(state: str) -> None:
    r = compute(_ret(state, Decimal(80_000)))
    assert r.state_result is not None
    assert r.state_result.state == state
    assert r.state_result.state_tax >= Decimal(0)


def test_pa_2023_flat_307_percent() -> None:
    # PA: no std deduction, flat 3.07% on AGI.
    r = compute(_ret("PA", Decimal(60_000)))
    assert r.state_result.state_tax == (Decimal(60_000) * Decimal("0.0307")).quantize(Decimal("0.01"))


def test_nc_2023_flat_475_percent_single() -> None:
    # Single std deduction $12,750 → taxable $37,250 × 4.75%.
    r = compute(_ret("NC", Decimal(50_000)))
    expected = (Decimal(50_000) - Decimal(12_750)) * Decimal("0.0475")
    assert r.state_result.state_tax == _q(expected)


def test_az_2023_flat_25_percent_mfj() -> None:
    # MFJ std deduction $27,700 → taxable $52,300 × 2.5%.
    r = compute(_ret("AZ", Decimal(80_000), FilingStatus.MFJ))
    expected = (Decimal(80_000) - Decimal(27_700)) * Decimal("0.025")
    assert r.state_result.state_tax == _q(expected)


def test_oh_2023_4_bracket_walk_single() -> None:
    # Single std deduction $2,400 → taxable $97,600.
    # 0% on first $26,050 = 0
    # 2.75% on next ($97,600 − $26,050) = $71,550 × 0.0275 = $1,967.625
    r = compute(_ret("OH", Decimal(100_000)))
    expected = Decimal("71550") * Decimal("0.0275")
    assert r.state_result.state_tax == _q(expected)


def test_mn_2023_2_bracket_walk_single() -> None:
    # Single std deduction $13,825 → taxable $46,175.
    # 5.35% on first $30,070 = $1,608.745
    # 6.80% on next ($46,175 − $30,070) = $16,105 × 0.068 = $1,095.14
    r = compute(_ret("MN", Decimal(60_000)))
    expected = (
        Decimal(30_070) * Decimal("0.0535")
        + (Decimal(46_175) - Decimal(30_070)) * Decimal("0.068")
    )
    assert r.state_result.state_tax == _q(expected)

