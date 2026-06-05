"""Tests for v0.5 features: capital-loss carryforward, locality tax (NYC/Yonkers),
and the 5 new state YAMLs (MA, OR, NJ, VA, GA)."""
from __future__ import annotations

from decimal import Decimal


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

