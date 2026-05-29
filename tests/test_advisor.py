"""Tests for the Tax Savings Advisor."""
from decimal import Decimal

from taxlens import compute
from taxlens.advisor import advise
from taxlens.advisor_multi import advise_multi
from taxlens.models import FilingStatus, Return


def _all_ids(recs):
    return {r.id for r in recs}


def test_advise_max_401k_for_w2_filer_with_zero_contribution():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(150_000),
    )
    recs = advise(ret, compute(ret))
    ids = _all_ids(recs)
    assert "max-401k" in ids
    rec = next(r for r in recs if r.id == "max-401k")
    # Marginal is 24% → savings ≈ 23000 × 0.24 = 5520.
    assert Decimal("5_000") <= rec.est_annual_savings <= Decimal("6_000")


def test_advise_backdoor_roth_warning_when_direct_contributed_over_limit():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(250_000),
        roth_ira_contributions=Decimal(7_000),
    )
    ids = _all_ids(advise(ret, compute(ret)))
    assert "backdoor-roth-warning" in ids


def test_advise_backdoor_roth_suggested_when_income_high_no_contribution():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(400_000),
    )
    ids = _all_ids(advise(ret, compute(ret)))
    assert "backdoor-roth" in ids


def test_advise_amt_planning_appears_when_amt_owed():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(240_000),
        iso_bargain_element=Decimal(400_000),
    )
    ids = _all_ids(advise(ret, compute(ret)))
    assert "amt-iso-staggering" in ids


def test_advise_s_corp_for_high_se_income():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        se_income=Decimal(180_000),
    )
    ids = _all_ids(advise(ret, compute(ret)))
    assert "s-corp-election" in ids


def test_advise_bunching_when_close_to_std_deduction():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(180_000),
        charitable_contributions=Decimal(8_000),
        mortgage_interest=Decimal(15_000),
        salt_paid=Decimal(12_000),
    )
    ids = _all_ids(advise(ret, compute(ret)))
    # Std ded (MFJ 2024) = 29200. Itemizable = 8000 + 15000 + min(12000, 10000) = 33000.
    # That's > 60% of std → bunching rule fires.
    assert "bunching-donations" in ids


def test_advise_estimated_tax_safe_harbor_when_owed_at_filing():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(200_000),                  # no withholding set → big owed at filing
    )
    ids = _all_ids(advise(ret, compute(ret)))
    assert "estimated-tax-safe-harbor" in ids


def test_advise_no_recs_for_minimal_return():
    """A genuinely simple, well-optimized return shouldn't produce spam."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(40_000),
        traditional_401k_contributions=Decimal(23_000),
        hsa_deduction=Decimal(4_150),
        federal_withholding=Decimal(3_000),
    )
    recs = advise(ret, compute(ret))
    assert len(recs) <= 1  # at most a low-signal note


def test_buggy_rule_does_not_crash_advisor(monkeypatch):
    """Defensive: a misbehaving individual rule must be skipped, not blow up the engine."""
    from taxlens import advisor as adv
    def boom(*a, **k): raise RuntimeError("kaboom")
    monkeypatch.setattr(adv, "ALL_RULES", [boom] + adv.ALL_RULES)
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE, wages=Decimal(100_000))
    advise(ret, compute(ret))  # must not raise


# ─── multi-year ────────────────────────────────────────────────────────────

def test_multi_persistent_refund_triggers():
    history = []
    for yr in (2023, 2024):
        ret = Return(
            tax_year=yr, filing_status=FilingStatus.MFJ,
            wages=Decimal(150_000), federal_withholding=Decimal(35_000),
        )
        history.append((ret, compute(ret)))
    ids = _all_ids(advise_multi(history))
    assert "reduce-overwithholding" in ids


def test_multi_roth_conv_window_in_low_income_year():
    history = []
    for yr, w in [(2023, 300_000), (2024, 50_000)]:
        ret = Return(tax_year=yr, filing_status=FilingStatus.MFJ, wages=Decimal(w))
        history.append((ret, compute(ret)))
    ids = _all_ids(advise_multi(history))
    assert any(i.startswith("roth-conv-") for i in ids)
