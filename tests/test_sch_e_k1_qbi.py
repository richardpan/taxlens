"""Tests for Schedule E, K-1 passthrough, QBI deduction, ISO AMT preference, CA MHST."""
from decimal import Decimal

from taxlens import compute
from taxlens.models import FilingStatus, Return


def test_rental_loss_allowed_for_active_participant_low_income():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(80_000),
        rental_net_income=Decimal(-15_000),
        is_active_real_estate_participant=True,
    )
    r = compute(ret)
    # Full $15k loss allowed (well under both $25k cap and $100k phaseout start).
    assert r.schedule_e_income == Decimal("-15000.00")
    assert r.passive_loss_disallowed == 0


def test_rental_loss_phased_out_at_high_income():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(140_000),                # phaseout: 100k start, 150k end
        rental_net_income=Decimal(-30_000),
        is_active_real_estate_participant=True,
    )
    r = compute(ret)
    # Allowance at 140k = 25000 − (140k−100k)×0.5 = 5000. Loss disallowed = 25000.
    assert r.schedule_e_income == Decimal("-5000.00")
    assert r.passive_loss_disallowed == Decimal("25000.00")


def test_rental_loss_disallowed_without_active_participation():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(60_000), rental_net_income=Decimal(-20_000),
    )
    r = compute(ret)
    assert r.schedule_e_income == 0
    assert r.passive_loss_disallowed == Decimal("20000.00")


def test_k1_qualified_divs_get_preferential_rate():
    # MFJ, low ordinary income, $50k qualified-div K-1: most should fall in 0% LTCG bracket.
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(50_000),
        k1_qualified_dividends=Decimal(50_000),
    )
    r = compute(ret)
    # Qualified bucket exists and is small (lots in 0% bracket).
    assert r.qualified_tax < Decimal(2_000)


def test_qbi_deduction_applies_to_k1_passthrough():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(80_000),
        k1_ordinary_business_income=Decimal(100_000),
        k1_section_199a_qbi=Decimal(100_000),
    )
    r = compute(ret)
    assert r.qbi_deduction > 0
    # Cap: 20% of (taxable − net cap gain). Taxable_pre_qbi ≈ 180k − 29.2k = 150.8k.
    # min(20k, 30.16k) = 20k. Allow some tolerance.
    assert Decimal("19000") <= r.qbi_deduction <= Decimal("20500")


def test_qbi_sstb_phased_out_above_threshold():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(0),
        k1_ordinary_business_income=Decimal(700_000),
        k1_section_199a_qbi=Decimal(700_000),
        k1_is_sstb=True,
    )
    r = compute(ret)
    # Threshold MFJ = 483_900; phaseout 100k → fully gone above 583_900.
    # Taxable ≈ 700k − 29.2k = 670.8k → above phaseout end → QBI fully phased out.
    assert r.qbi_deduction == 0


def test_iso_bargain_element_triggers_amt():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(240_000),
        iso_bargain_element=Decimal(400_000),
    )
    r = compute(ret)
    assert r.amt > 0


def test_ca_mhst_kicks_in_over_1m():
    # MFJ, $1.2M wages → CA taxable comfortably > $1M, MHST applies on the excess.
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(1_200_000),
        state="CA",
    )
    r = compute(ret)
    sr = r.state_result
    # State taxable ≈ 1_188_920. MHST = 188_920 × 1% ≈ 1889.20.
    # Verify MHST step exists in the audit trail.
    assert any("Mental Health" in s.label for s in sr.steps)
    # Without MHST, top-bracket CA tax on 1.2M would be ~111k; MHST adds another ~1.9k.
    assert sr.state_tax > Decimal(100_000)
