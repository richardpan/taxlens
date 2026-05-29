"""Education credits (Form 8863) — AOTC + Lifetime Learning Credit."""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


def _ret(**kwargs):
    base = dict(tax_year=2024, filing_status=FilingStatus.SINGLE,
                wages=Decimal(60000))
    base.update(kwargs)
    return Return(**base)


# ── AOTC math ───────────────────────────────────────────────────────────────

def test_aotc_one_student_full_credit():
    # $4,000 expenses → 100% of first $2,000 + 25% of next $2,000 = $2,500
    ret = _ret(aotc_qualified_expenses=[Decimal(4000)])
    r = compute(ret)
    assert r.aotc_nonrefundable + r.aotc_refundable == Decimal("2500.00")
    assert r.aotc_refundable == Decimal("1000.00")     # 40% refundable
    assert r.aotc_nonrefundable == Decimal("1500.00")


def test_aotc_partial_first_tier_only():
    # $1,500 expenses → 100% = $1,500
    ret = _ret(aotc_qualified_expenses=[Decimal(1500)])
    r = compute(ret)
    assert r.aotc_nonrefundable + r.aotc_refundable == Decimal("1500.00")


def test_aotc_capped_at_4000_per_student():
    # $10,000 expenses → capped at $4,000 → $2,500 credit
    ret = _ret(aotc_qualified_expenses=[Decimal(10000)])
    r = compute(ret)
    assert r.aotc_nonrefundable + r.aotc_refundable == Decimal("2500.00")


def test_aotc_multiple_students():
    # 2 students at full $4k each → 2 × $2,500 = $5,000
    ret = _ret(aotc_qualified_expenses=[Decimal(4000), Decimal(4000)])
    r = compute(ret)
    assert r.aotc_nonrefundable + r.aotc_refundable == Decimal("5000.00")


# ── LLC math ────────────────────────────────────────────────────────────────

def test_llc_full_credit():
    # $10,000 expenses × 20% = $2,000
    ret = _ret(llc_qualified_expenses=Decimal(10000))
    r = compute(ret)
    assert r.llc_credit == Decimal("2000.00")


def test_llc_partial():
    # $3,000 × 20% = $600
    ret = _ret(llc_qualified_expenses=Decimal(3000))
    r = compute(ret)
    assert r.llc_credit == Decimal("600.00")


def test_llc_capped_at_10000_expenses():
    ret = _ret(llc_qualified_expenses=Decimal(50000))
    r = compute(ret)
    assert r.llc_credit == Decimal("2000.00")


# ── phaseout ────────────────────────────────────────────────────────────────

def test_education_phaseout_single_midpoint():
    # Single phaseout 80k-90k. At 85k MAGI → 50% factor
    ret = _ret(wages=Decimal(85000), aotc_qualified_expenses=[Decimal(4000)])
    r = compute(ret)
    total = r.aotc_nonrefundable + r.aotc_refundable
    # 2500 * 0.5 = 1250
    assert total == Decimal("1250.00")


def test_education_above_phaseout_zero():
    ret = _ret(wages=Decimal(100000), aotc_qualified_expenses=[Decimal(4000)],
               llc_qualified_expenses=Decimal(5000))
    r = compute(ret)
    assert r.aotc_nonrefundable == Decimal("0")
    assert r.aotc_refundable == Decimal("0")
    assert r.llc_credit == Decimal("0")


def test_education_mfj_phaseout_window_doubled():
    # MFJ phaseout 160k-180k. At 170k MAGI → 50% factor
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(170000),
               aotc_qualified_expenses=[Decimal(4000)])
    r = compute(ret)
    assert r.aotc_nonrefundable + r.aotc_refundable == Decimal("1250.00")


# ── disqualifiers ───────────────────────────────────────────────────────────

def test_education_mfs_disallowed():
    ret = _ret(filing_status=FilingStatus.MFS,
               aotc_qualified_expenses=[Decimal(4000)],
               llc_qualified_expenses=Decimal(5000))
    r = compute(ret)
    assert r.aotc_nonrefundable == Decimal("0")
    assert r.aotc_refundable == Decimal("0")
    assert r.llc_credit == Decimal("0")


# ── refundability of AOTC ───────────────────────────────────────────────────

def test_aotc_refundable_portion_pushes_refund():
    # Low-income filer with no tax liability still gets the 40% refundable AOTC.
    ret = _ret(wages=Decimal(8000), aotc_qualified_expenses=[Decimal(4000)])
    r = compute(ret)
    assert r.total_tax == Decimal("0")
    assert r.aotc_refundable == Decimal("1000.00")
    # Nonrefundable can't be used (no tax to offset) — but refundable still flows.
    assert r.refund_or_owed >= Decimal("1000")


# ── stacking with EITC ──────────────────────────────────────────────────────

def test_aotc_stacks_with_eitc_for_refund():
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(25000),
               qualifying_children=2, aotc_qualified_expenses=[Decimal(4000)])
    r = compute(ret)
    assert r.eitc > Decimal("5000")
    assert r.aotc_refundable == Decimal("1000.00")
    # Refund includes both
    assert r.refund_or_owed >= r.eitc + r.aotc_refundable
