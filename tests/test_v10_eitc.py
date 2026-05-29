"""EITC (Schedule EIC) — refundable Earned Income Tax Credit tests."""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


def _ret(**kwargs):
    base = dict(tax_year=2024, filing_status=FilingStatus.SINGLE)
    base.update(kwargs)
    return Return(**base)


# ── happy path: plateau (max credit) for 2 kids, single ─────────────────────

def test_eitc_max_credit_two_kids_single():
    # Earned income at the plateau but below phase-out start (single, 2 kids: $22,720)
    ret = _ret(wages=Decimal(20000), qualifying_children=2)
    r = compute(ret)
    assert r.eitc == Decimal("6960.00")


def test_eitc_max_credit_three_kids_mfj():
    # MFJ, 3 kids, plateau: earned 25k, well under joint phaseout start 29,640
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(25000), qualifying_children=3)
    r = compute(ret)
    assert r.eitc == Decimal("7830.00")


# ── phase-in: earned income below earned_income_amount ──────────────────────

def test_eitc_phase_in_one_kid():
    # 1 kid, single, earned $5,000. Phase-in rate = 4213/12390 ≈ 0.340.
    ret = _ret(wages=Decimal(5000), qualifying_children=1)
    r = compute(ret)
    expected = (Decimal(5000) * Decimal("4213") / Decimal("12390")).quantize(Decimal("0.01"))
    assert r.eitc == expected


# ── phase-out region: linear decrease past start ────────────────────────────

def test_eitc_phaseout_two_kids_single():
    # Single, 2 kids, wages $40,000 → past phaseout start (22,720), below completed (55,768)
    ret = _ret(wages=Decimal(40000), qualifying_children=2)
    r = compute(ret)
    reduction = (Decimal(40000) - Decimal(22720)) * Decimal("0.2106")
    expected = (Decimal(6960) - reduction).quantize(Decimal("0.01"))
    assert r.eitc == expected


def test_eitc_fully_phased_out():
    # Past completed_phaseout: no credit.
    ret = _ret(wages=Decimal(60000), qualifying_children=2)
    r = compute(ret)
    assert r.eitc == Decimal("0")


# ── disqualifiers ───────────────────────────────────────────────────────────

def test_eitc_mfs_disallowed():
    ret = _ret(filing_status=FilingStatus.MFS, wages=Decimal(20000), qualifying_children=2)
    r = compute(ret)
    assert r.eitc == Decimal("0")


def test_eitc_investment_income_over_limit():
    # 2024 limit = $11,600. Set interest > limit; no EITC.
    ret = _ret(wages=Decimal(20000), qualifying_children=2,
               interest_income=Decimal(12000))
    r = compute(ret)
    assert r.eitc == Decimal("0")


def test_eitc_zero_earned_income():
    # No wages, no SE — pure investment / retirement income gets nothing.
    ret = _ret(wages=Decimal(0), interest_income=Decimal(500), qualifying_children=1)
    r = compute(ret)
    assert r.eitc == Decimal("0")


# ── childless EITC (small but real) ─────────────────────────────────────────

def test_eitc_childless_single_plateau():
    # Single, 0 kids, plateau: $8,260–$10,330 earned → max $632
    ret = _ret(wages=Decimal(9000), qualifying_children=0)
    r = compute(ret)
    assert r.eitc == Decimal("632.00")


# ── refundability: credit flows into refund ─────────────────────────────────

def test_eitc_is_refundable_pushes_refund_positive():
    # Low-income MFJ with 2 kids, $0 withholding. EITC should result in a refund
    # even though total tax is ~$0.
    ret = _ret(filing_status=FilingStatus.MFJ, wages=Decimal(25000),
               qualifying_children=2, federal_withholding=Decimal(0))
    r = compute(ret)
    assert r.total_tax == Decimal("0")
    assert r.eitc > Decimal("5000")
    # Refund includes EITC (and ACTC since we have 2 kids + earned income).
    assert r.refund_or_owed >= r.eitc


# ── 2023 parameter sanity ───────────────────────────────────────────────────

def test_eitc_2023_two_kids_plateau():
    ret = Return(tax_year=2023, filing_status=FilingStatus.SINGLE,
                 wages=Decimal(19000), qualifying_children=2)
    r = compute(ret)
    assert r.eitc == Decimal("6604.00")
