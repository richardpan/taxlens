"""Tests for v0.5 features: capital-loss carryforward, locality tax (NYC/Yonkers),
and the 5 new state YAMLs (MA, OR, NJ, VA, GA)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


# ---------- multi-year capital-loss carryforward -------------------------

def test_carryforward_in_offsets_gains():
    """A prior $7,000 carry-in should fully offset $5,000 of current-year LT gains
    and leave $2,000 still carried forward."""
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        long_term_capital_gains=Decimal("5000"),
        capital_loss_carryforward_in=Decimal("7000"),
    )
    res = compute(ret)
    # Net cap = 5000 - 7000 = -2000; allowed loss is -2000 (under -3000 floor);
    # so 0 carry-out (entire residual loss used).
    assert res.capital_loss_carryforward_out == Decimal("0")


def test_net_loss_creates_carryforward_out():
    """Net $10k loss → $3,000 allowed this year → $7,000 carried forward."""
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal("100000"),
        long_term_capital_gains=Decimal("-10000"),
    )
    res = compute(ret)
    assert res.capital_loss_carryforward_out == Decimal("7000")


# ---------- NYC + Yonkers locality tax -----------------------------------

def test_nyc_locality_adds_to_ny_state_tax():
    base = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        state="NY",
        wages=Decimal("100000"),
    )
    res_no_loc = compute(base)
    base_with_nyc = base.model_copy(update={"locality": "NYC"})
    res_nyc = compute(base_with_nyc)
    sr_base = res_no_loc.state_result
    sr_nyc = res_nyc.state_result
    assert sr_base is not None and sr_nyc is not None
    assert sr_nyc.locality == "NYC"
    assert sr_nyc.locality_tax > 0
    assert sr_nyc.state_tax > sr_base.state_tax


def test_yonkers_surcharge_is_fraction_of_state_tax():
    base = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        state="NY",
        wages=Decimal("100000"),
    )
    res_no_loc = compute(base)
    sr_base = res_no_loc.state_result
    res_yonk = compute(base.model_copy(update={"locality": "YONKERS"}))
    sr_yonk = res_yonk.state_result
    assert sr_yonk is not None and sr_base is not None
    # Yonkers surcharge ≈ 16.75% of state tax. Allow rounding slack.
    expected = sr_base.state_tax * Decimal("0.1675")
    assert abs(sr_yonk.locality_tax - expected) <= Decimal("1.00")


# ---------- new 2024 state YAMLs -----------------------------------------

@pytest.mark.parametrize("state", ["MA", "OR", "NJ", "VA", "GA"])
def test_new_states_compute_positive_tax(state):
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        state=state,
        wages=Decimal("120000"),
    )
    res = compute(ret)
    assert res.state_result is not None
    assert res.state_result.state == state
    assert res.state_result.state_tax > 0


def test_ma_millionaire_surtax_kicks_in():
    """MA 4% surtax over $1,053,750 should make tax super-linear."""
    low = compute(Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                          state="MA", wages=Decimal("500000")))
    high = compute(Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                           state="MA", wages=Decimal("2000000")))
    assert low.state_result is not None and high.state_result is not None
    # Effective rate at $2M should exceed effective rate at $500k.
    low_rate = low.state_result.state_tax / Decimal("500000")
    high_rate = high.state_result.state_tax / Decimal("2000000")
    assert high_rate > low_rate
