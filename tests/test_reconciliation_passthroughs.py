"""Reconciliation passthrough caps + floor — close gaps for items the
engine can't fully recover from extracted PDF inputs.

* ``child_tax_credit_reported`` (1040 line 19) caps the modeled
  nonrefundable CTC + ODC. Common cause of overstatement: dependents
  claimed under ODC instead of CTC, or the filer affirmatively elected
  to forgo the credit.
* ``additional_ctc_reported`` (1040 line 28) caps modeled refundable ACTC.
* ``deduction_reported`` (1040 line 12) FLOORS the engine-resolved
  deduction so age 65+ / blind additional standard deduction (§63(f))
  still closes the gap even though we don't yet model the underlying
  age / blindness flags.
"""

from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def _base_return(**overrides):
    base = dict(
        tax_year=2024,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("220000"),
        qualifying_children=1,
    )
    base.update(overrides)
    return Return(**base)


def test_ctc_capped_at_reported_line_19():
    """Engine would model $2,000 CTC for one child; capping at $0
    reflects a return where the dependent was claimed only under the
    Credit for Other Dependents."""
    rules = load_rules(2024)
    no_cap = compute(_base_return(), rules)
    capped = compute(
        _base_return(child_tax_credit_reported=Decimal("0")), rules
    )
    assert no_cap.credits == Decimal("2000.00")
    assert capped.credits == Decimal("0.00")
    assert capped.total_tax - no_cap.total_tax == Decimal("2000.00")


def test_ctc_cap_does_not_inflate_when_reported_exceeds_modeled():
    """Cap is one-way: a higher reported value never raises the engine's
    own modeled credit (would mask real engine bugs)."""
    rules = load_rules(2024)
    capped = compute(
        _base_return(child_tax_credit_reported=Decimal("9999")), rules
    )
    assert capped.credits == Decimal("2000.00")


def test_actc_capped_at_reported_line_28():
    """Force ACTC > 0 by using low-tax inputs that leave CTC unused, then
    verify the reported line-28 cap clamps it down."""
    rules = load_rules(2024)
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal("30000"), qualifying_children=2,
    )
    no_cap = compute(ret, rules)
    capped = compute(
        ret.model_copy(update={"additional_ctc_reported": Decimal("0")}),
        rules,
    )
    assert no_cap.actc > Decimal("0")
    assert capped.actc == Decimal("0")


def test_deduction_floor_handles_age_65_extra():
    """The +$1,550 (TY2024 MFJ one-spouse-65+) additional standard
    deduction isn't yet modeled. With ``deduction_reported`` set, the
    engine uses the printed value as a floor and reconciliation closes."""
    rules = load_rules(2024)
    no_floor = compute(_base_return(), rules)
    with_floor = compute(
        _base_return(deduction_reported=Decimal("31100")),  # 29200 + 1550 + 350
        rules,
    )
    assert no_floor.deduction_used == Decimal("29200.00")
    assert with_floor.deduction_used == Decimal("31100.00")
    assert with_floor.total_tax < no_floor.total_tax


def test_deduction_floor_ignores_smaller_reported_value():
    """A reported deduction smaller than what the engine resolved is
    ignored — engine's SALT-capped + Pease-reduced math is preferred
    over trusting a potentially mis-extracted lower value."""
    rules = load_rules(2024)
    res = compute(
        _base_return(deduction_reported=Decimal("100")), rules
    )
    assert res.deduction_used == Decimal("29200.00")


def test_st_loss_offsets_lt_in_qualified_stack():
    """Net short-term loss must offset net long-term gain BEFORE the
    qualified-rate stack (Schedule D / Qualified Dividends and Capital
    Gain Tax Worksheet). Without this, a return with $5k LT gain +
    $8k ST loss would over-tax the LT gain at preferential rates."""
    rules = load_rules(2022)
    ret_with_st_loss = Return(
        tax_year=2022, filing_status=FilingStatus.MFJ,
        wages=Decimal("200000"),
        long_term_capital_gains=Decimal("5710"),
        short_term_capital_gains=Decimal("-8465"),
        qualified_dividends=Decimal("2565"),
    )
    ret_no_st_loss = ret_with_st_loss.model_copy(
        update={"short_term_capital_gains": Decimal("0")}
    )
    res_with = compute(ret_with_st_loss, rules)
    res_no = compute(ret_no_st_loss, rules)
    # ST loss offsets LT entirely, so qual-rate income drops from
    # ($2565 + $5710) to ($2565 + 0). Qualified tax falls by exactly
    # the LT-portion-times-rate. The ordinary side also drops because
    # the net ST loss flows through AGI as a (capped) deduction — but
    # the KEY invariant is that the LT $5,710 is no longer taxed at
    # the preferential rate.
    qual_drop = res_no.qualified_tax - res_with.qualified_tax
    assert qual_drop == Decimal("5710") * Decimal("0.15")
