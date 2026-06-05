"""Tests for Roth conversion + Tax-Loss Harvest simulators and WA cap-gains tax."""
from __future__ import annotations

from decimal import Decimal

from taxlens.models import FilingStatus, Return
from taxlens.simulators import simulate_roth_conversion, simulate_tax_loss_harvest


def _ret(**kw) -> Return:
    base = dict(tax_year=2024, filing_status=FilingStatus.SINGLE)
    base.update(kw)
    return Return(**base)


def test_roth_conversion_increases_tax():
    base = _ret(wages=Decimal("80000"))
    sim = simulate_roth_conversion(base, Decimal("20000"))
    assert sim.tax_delta > Decimal("0")
    # At ~$80k income, marginal rate on the conversion should be around 22%.
    rate = float(sim.federal_marginal_rate)
    assert 0.15 < rate < 0.30


def test_zero_conversion_is_noop():
    base = _ret(wages=Decimal("80000"))
    sim = simulate_roth_conversion(base, Decimal("0"))
    assert sim.tax_delta == Decimal("0")


def test_tlh_reduces_tax_when_offsetting_gains():
    base = _ret(wages=Decimal("120000"), long_term_capital_gains=Decimal("15000"))
    sim = simulate_tax_loss_harvest(base, Decimal("10000"))
    assert sim.tax_delta < Decimal("0")


def test_tlh_caps_at_3000_against_ordinary():
    # Base has $2k LT gains; harvest $3k → net -1k loss, fully usable.
    # Harvest $10k → net -8k, but only $3k offsets ordinary income this year
    # (the rest carries forward — TaxLens engine doesn't yet model carryover).
    base = _ret(wages=Decimal("120000"), long_term_capital_gains=Decimal("2000"))
    sim_small = simulate_tax_loss_harvest(base, Decimal("5000"))   # net -3000
    sim_big = simulate_tax_loss_harvest(base, Decimal("12000"))    # net -10000 → capped
    # Same-year tax benefit should be approximately equal (extra carries forward).
    delta_small = sim_small.tax_delta
    delta_big = sim_big.tax_delta
    # Allow a small tolerance for the LTCG-vs-ordinary rate differential on the
    # first $2k of offset gains.
    assert abs(delta_small - delta_big) < Decimal("500")
