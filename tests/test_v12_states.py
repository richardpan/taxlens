"""v0.12.0 state expansion — verify all 8 new state YAMLs load and compute."""
from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


@pytest.mark.parametrize("state", ["NV", "SD", "WY", "AK", "TN", "NH"])
def test_no_tax_states_load_and_return_zero(state):
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(100_000), state=state,
    )
    r = compute(ret)
    assert r.state_result is not None
    assert r.state_result.state == state
    assert r.state_result.state_tax == Decimal("0")


def test_wisconsin_4_bracket_walk_single():
    # Single, $50,000 wages. Standard deduction $12,760 → taxable $37,240
    # Brackets: 3.5% on first $14,320 = $501.20
    # 4.4% on next ($28,640 − $14,320) = $14,320 × 0.044 = $630.08
    # 5.3% on remaining ($37,240 − $28,640) = $8,600 × 0.053 = $455.80
    # Total = $1,587.08
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(50000), state="WI",
    )
    r = compute(ret)
    expected = (
        Decimal(14320) * Decimal("0.035")
        + Decimal(14320) * Decimal("0.044")
        + Decimal(8600) * Decimal("0.053")
    )
    assert r.state_result.state_tax == expected.quantize(Decimal("0.01"))


def test_indiana_flat_rate():
    # Single, $60,000 wages, std ded $1,000 → taxable $59,000 × 3.05%
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(60000), state="IN",
    )
    r = compute(ret)
    expected = Decimal(59000) * Decimal("0.0305")
    assert r.state_result.state_tax == expected.quantize(Decimal("0.01"))
