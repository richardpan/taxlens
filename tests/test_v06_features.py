"""Tests for v0.6: ODC credit."""
from __future__ import annotations

from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
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
