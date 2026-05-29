"""Tests for v0.6: 8 new state YAMLs, 2023 backfills for v0.5 states, ODC."""
from __future__ import annotations

from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


@pytest.mark.parametrize("state", ["PA", "OH", "NC", "AZ", "MN", "CO", "MI", "MD"])
def test_new_states_2024(state):
    res = compute(Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                          state=state, wages=Decimal("100000")))
    assert res.state_result is not None
    assert res.state_result.state == state
    # PA / OH single-bracket states still produce positive tax on 100k wages
    assert res.state_result.state_tax > 0


@pytest.mark.parametrize("state", ["MA", "OR", "NJ", "VA", "GA"])
def test_2023_backfills(state):
    res = compute(Return(tax_year=2023, filing_status=FilingStatus.SINGLE,
                          state=state, wages=Decimal("80000")))
    assert res.state_result is not None
    assert res.state_result.state_tax > 0


def test_ga_2024_lower_than_2023():
    """GA cut top rate 5.75% (graduated) -> 5.39% (flat). 2024 should be lower
    on the same income."""
    r23 = compute(Return(tax_year=2023, filing_status=FilingStatus.SINGLE,
                          state="GA", wages=Decimal("100000")))
    r24 = compute(Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                          state="GA", wages=Decimal("100000")))
    assert r24.state_result.state_tax < r23.state_result.state_tax


def test_odc_credit_added():
    """$500 ODC per other_dependent should reduce total tax."""
    base = Return(tax_year=2024, filing_status=FilingStatus.MFJ, wages=Decimal("120000"))
    with_dep = base.model_copy(update={"other_dependents": 2})
    r0 = compute(base)
    r1 = compute(with_dep)
    # 2 dependents × $500 = $1,000 credit, fully usable on this income.
    diff = r0.total_tax - r1.total_tax
    assert diff == Decimal("1000")


def test_ctc_and_odc_combined_phaseout():
    """1 child + 1 other dependent at high income → combined credit phases out."""
    high = Return(tax_year=2024, filing_status=FilingStatus.MFJ,
                   wages=Decimal("500000"), qualifying_children=1, other_dependents=1)
    res = compute(high)
    # Raw 2000+500 = 2500. Phaseout starts at 400k MFJ, $50/$1k over.
    # Over by 100k -> 100 × $50 = $5000 reduction. Fully phased out (credit = 0).
    # But because credits are bundled, check overall credits is 0 attributable to CTC.
    # We can at least assert: no improvement vs same return with no dependents.
    none = high.model_copy(update={"qualifying_children": 0, "other_dependents": 0})
    res_none = compute(none)
    assert res.total_tax == res_none.total_tax  # both phased out fully
