"""Saver's Credit (Form 8880), ACTC (Form 8812), Premium Tax Credit (Form 8962)."""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


def _ret(**kwargs):
    base = dict(tax_year=2024, filing_status=FilingStatus.SINGLE,
                wages=Decimal(20000))
    base.update(kwargs)
    return Return(**base)


# ── Saver's Credit (Form 8880) ──────────────────────────────────────────────

def test_savers_50pct_tier_single():
    # Single, AGI ≤ $23,000 → 50% rate. $1,500 IRA contrib → $750 credit
    ret = _ret(wages=Decimal(20000), traditional_ira_contributions=Decimal(1500))
    r = compute(ret)
    assert r.savers_credit == Decimal("750.00")


def test_savers_capped_at_2000_per_person():
    # Single, $5k contribs → capped at $2k → 50% = $1,000
    ret = _ret(wages=Decimal(20000), traditional_401k_contributions=Decimal(5000))
    r = compute(ret)
    assert r.savers_credit == Decimal("1000.00")


def test_savers_mfj_doubled_cap():
    # MFJ, $5k contribs → cap is $4k → 50% (at AGI ≤ $46k) = $2,000
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(30000),
               traditional_401k_contributions=Decimal(5000))
    r = compute(ret)
    assert r.savers_credit == Decimal("2000.00")


def test_savers_10pct_tier():
    # Single AGI between $25k–$38,250 → 10% rate. $2k contrib → $200
    ret = _ret(wages=Decimal(35000), roth_ira_contributions=Decimal(2000))
    r = compute(ret)
    assert r.savers_credit == Decimal("200.00")


def test_savers_above_phaseout_zero():
    # Single AGI > $38,250 → 0%
    ret = _ret(wages=Decimal(50000), traditional_401k_contributions=Decimal(2000))
    r = compute(ret)
    assert r.savers_credit == Decimal("0")


def test_savers_no_contribs_zero():
    ret = _ret(wages=Decimal(20000))
    r = compute(ret)
    assert r.savers_credit == Decimal("0")


# ── ACTC (Form 8812 refundable Additional CTC) ──────────────────────────────

def test_actc_refundable_when_ctc_exceeds_tax():
    # Low-income MFJ with 2 kids: tax liability ~$0, so most of $4k CTC becomes ACTC
    # (capped at $1,700 × 2 = $3,400, also capped at 15% × (earned − $2,500))
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(25000),
               qualifying_children=2)
    r = compute(ret)
    earnings_test = (Decimal(25000) - Decimal(2500)) * Decimal("0.15")
    expected_actc = min(Decimal(4000), Decimal(3400), earnings_test).quantize(Decimal("0.01"))
    assert r.actc == expected_actc


def test_actc_capped_by_earnings_test():
    # Very low earned income → 15% × (earned − $2,500) becomes the binding cap
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(6000),
               qualifying_children=2)
    r = compute(ret)
    expected = (Decimal(6000) - Decimal(2500)) * Decimal("0.15")
    assert r.actc == expected.quantize(Decimal("0.01"))


def test_actc_zero_when_ctc_fully_used_nonrefundable():
    # High-income single with 1 kid: tax > $2,000 CTC → entirely nonref, ACTC = 0
    ret = _ret(wages=Decimal(80000), qualifying_children=1)
    r = compute(ret)
    assert r.actc == Decimal("0")


def test_actc_zero_when_no_earned_income_over_2500():
    # Earned income at exactly $2,500 → 15% test = 0
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(2500),
               qualifying_children=2)
    r = compute(ret)
    assert r.actc == Decimal("0")


# ── Premium Tax Credit (Form 8962) ──────────────────────────────────────────

def test_ptc_simple_refundable_credit():
    # MFJ, 2 ppl, $40k AGI → ~200% FPL. SLCSP $10k/year, paid $9k, no APTC.
    # Applicable Figure at 200% = 2% → contribution = $40k × 0.02 = $800
    # PTC = $10k − $800 = $9,200, capped at $9k plan premium.
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(40000),
               marketplace_household_size=2,
               marketplace_slcsp_annual=Decimal(10000),
               marketplace_plan_premium_annual=Decimal(9000))
    r = compute(ret)
    assert r.ptc_net == Decimal("9000.00")
    assert r.ptc_excess_aptc_repayment == Decimal("0")


def test_ptc_reconciliation_with_aptc_exactly_matches():
    # APTC = computed PTC → net 0, no repayment
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(40000),
               marketplace_household_size=2,
               marketplace_slcsp_annual=Decimal(10000),
               marketplace_plan_premium_annual=Decimal(9000),
               marketplace_advance_ptc_paid=Decimal(9000))
    r = compute(ret)
    assert r.ptc_net == Decimal("0")
    assert r.ptc_excess_aptc_repayment == Decimal("0")


def test_ptc_excess_aptc_repayment_capped():
    # APTC > computed PTC → must repay difference, but capped by FPL bucket
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(40000),
               marketplace_household_size=2,
               marketplace_slcsp_annual=Decimal(10000),
               marketplace_plan_premium_annual=Decimal(9000),
               marketplace_advance_ptc_paid=Decimal(15000))
    r = compute(ret)
    # Excess = 15000 − 9000 = $6,000, but at ~200% FPL (family) cap = $1,950
    # (200-300% bucket: family cap $1,950)
    assert r.ptc_excess_aptc_repayment == Decimal("1950.00")


def test_ptc_above_400_fpl_no_cliff():
    # Single, $80k → roughly 550% FPL. Post-ARPA/IRA: still eligible at 8.5%.
    # Contribution = $80k × 0.085 = $6,800. If SLCSP $12k, PTC = $5,200.
    ret = _ret(wages=Decimal(80000),
               marketplace_household_size=1,
               marketplace_slcsp_annual=Decimal(12000),
               marketplace_plan_premium_annual=Decimal(12000))
    r = compute(ret)
    expected = Decimal(12000) - Decimal(80000) * Decimal("0.085")
    assert r.ptc_net == expected.quantize(Decimal("0.01"))


def test_ptc_no_marketplace_no_op():
    ret = _ret(wages=Decimal(40000))
    r = compute(ret)
    assert r.ptc_net == Decimal("0")
    assert r.ptc_excess_aptc_repayment == Decimal("0")


def test_ptc_excess_repayment_increases_total_tax():
    # Excess APTC repayment should add to total_tax
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(40000),
               marketplace_household_size=2,
               marketplace_slcsp_annual=Decimal(10000),
               marketplace_plan_premium_annual=Decimal(9000),
               marketplace_advance_ptc_paid=Decimal(15000))
    r_with = compute(ret)
    r_baseline = compute(_ret(filing_status=FilingStatus.MFJ, wages=Decimal(40000)))
    assert r_with.total_tax > r_baseline.total_tax
    # Total tax should include the $1,950 capped repayment
    assert r_with.total_tax - r_baseline.total_tax == Decimal("1950.00")
