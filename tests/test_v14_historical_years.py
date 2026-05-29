"""v0.14.0 — Historical accuracy golden tests for TY2015-2022.

For each year we verify a single-filer, no-kids, $75k wages baseline computation
against hand-calculated values derived directly from the IRS Rev. Proc. for
that year. This pins the bracket walk, standard deduction, and (pre-TCJA)
personal exemption arithmetic so future engine refactors can't silently
change historical results.

We also spot-check:
  - 2017→2018 TCJA delta (rates dropped, SD nearly doubled, PE eliminated)
  - Pre-TCJA CTC at $1,000 with $3,000 earned threshold (no per-kid refund cap)
  - Pre-TCJA Pease limitation
  - 2021 ARPA CTC (simplified: $3,000/kid fully refundable; documented caveat)
"""
from decimal import Decimal

import pytest

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return


# (year, expected_taxable_income, expected_ordinary_tax) for a single filer,
# $75k wages, no dependents, no itemize, no other income. Hand-calculated.
BASELINE = [
    (2015, Decimal("64700.00"), Decimal("11968.75")),
    (2016, Decimal("64650.00"), Decimal("11933.75")),
    (2017, Decimal("64600.00"), Decimal("11888.75")),
    (2018, Decimal("63000.00"), Decimal("9799.50")),   # first TCJA year
    (2019, Decimal("62800.00"), Decimal("9674.50")),
    (2020, Decimal("62600.00"), Decimal("9562.00")),
    (2021, Decimal("62450.00"), Decimal("9487.50")),
    (2022, Decimal("62050.00"), Decimal("9268.00")),
]


@pytest.mark.parametrize("year,expected_ti,expected_tax", BASELINE)
def test_single_75k_baseline(year, expected_ti, expected_tax):
    ret = Return(tax_year=year, filing_status=FilingStatus.SINGLE, wages=Decimal(75000))
    r = compute(ret)
    assert r.agi == Decimal("75000.00"), f"TY{year} AGI"
    assert r.taxable_income == expected_ti, f"TY{year} taxable_income"
    assert r.ordinary_tax == expected_tax, f"TY{year} ordinary_tax"
    assert r.total_tax == expected_tax, f"TY{year} total_tax (no other components)"


def test_tcja_boundary_2017_vs_2018():
    """TCJA dramatically dropped federal tax. A $100k single filer should pay
    materially less in 2018 than 2017."""
    r17 = compute(Return(tax_year=2017, filing_status=FilingStatus.SINGLE, wages=Decimal(100000)))
    r18 = compute(Return(tax_year=2018, filing_status=FilingStatus.SINGLE, wages=Decimal(100000)))
    # 2017: SD $6,350 + PE $4,050 → TI $89,600. 2018: SD $12,000 + no PE → TI $88,000.
    assert r17.personal_exemption_used == Decimal("4050.00")
    assert r18.personal_exemption_used == Decimal("0.00")
    # TCJA was a tax cut at this income; verify 2018 is materially lower.
    assert r18.total_tax < r17.total_tax - Decimal(2000), (
        f"TCJA savings too small: 2017={r17.total_tax}, 2018={r18.total_tax}"
    )


def test_pre_tcja_personal_exemption_with_dependents():
    """TY2016 MFJ with 2 dependents → 4 exemptions × $4,050 = $16,200."""
    ret = Return(
        tax_year=2016, filing_status=FilingStatus.MFJ,
        wages=Decimal(80000), qualifying_children=2,
    )
    r = compute(ret)
    assert r.personal_exemption_used == Decimal("16200.00")


def test_pre_tcja_ctc_uses_3k_earned_threshold():
    """TY2017 single $20k wages, 2 kids: CTC = $2,000, refundable ACTC = 15% × (20k − 3k) = $2,550,
    capped at $2,000 total → $2,000 refundable (no per-kid $1,400 cap pre-2018)."""
    ret = Return(
        tax_year=2017, filing_status=FilingStatus.HOH,
        wages=Decimal(20000), qualifying_children=2,
    )
    r = compute(ret)
    # AGI $20k; SD $9,350; PE 3×$4,050=$12,150 → TI = 0 → no income tax.
    # Full $2,000 CTC is unused as nonrefundable → flows to refundable ACTC.
    # ACTC earnings test = (20000 − 3000) × 0.15 = $2,550, capped at total CTC $2,000.
    assert r.actc == Decimal("2000.00"), f"ACTC should be $2,000, got {r.actc}"
    assert r.refund_or_owed >= Decimal("2000.00")


def test_pre_tcja_pease_limitation_kicks_in():
    """TY2017 MFJ AGI $500k with $50k itemized deductions: Pease cuts itemized by
    3% × (500k − 313.8k) = 3% × $186,200 = $5,586."""
    ret = Return(
        tax_year=2017, filing_status=FilingStatus.MFJ,
        wages=Decimal(500000),
        itemized_deductions=Decimal(50000),
    )
    r = compute(ret)
    assert r.pease_reduction == Decimal("5586.00")
    # Deduction used = $50,000 − $5,586 = $44,414 (since > std $12,700)
    assert r.deduction_used == Decimal("44414.00")
    assert r.deduction_kind == "itemized"


def test_post_tcja_no_pease_no_pe():
    """TY2019 high-income filer should have zero Pease and zero personal exemption."""
    ret = Return(
        tax_year=2019, filing_status=FilingStatus.MFJ,
        wages=Decimal(500000),
        itemized_deductions=Decimal(50000),
    )
    r = compute(ret)
    assert r.pease_reduction == Decimal("0.00")
    assert r.personal_exemption_used == Decimal("0.00")
    assert r.deduction_used == Decimal("50000.00")


def test_2021_arpa_ctc_fully_refundable():
    """TY2021 with ARPA: $3,000/kid fully refundable. Low-income MFJ should get
    full $6,000 refund for 2 kids (no per-kid cap)."""
    ret = Return(
        tax_year=2021, filing_status=FilingStatus.MFJ,
        wages=Decimal(25000), qualifying_children=2,
    )
    r = compute(ret)
    # 2 × $3,000 = $6,000 raw CTC. With no income tax owed at this level,
    # all $6k should flow as ACTC (fully refundable in 2021).
    assert r.actc == Decimal("6000.00"), f"ARPA CTC should be $6,000, got {r.actc}"


def test_2020_amt_exemption_post_tcja():
    """TY2020 AMT exemption single = $72,900. High AMTI should produce AMT."""
    ret = Return(
        tax_year=2020, filing_status=FilingStatus.SINGLE,
        wages=Decimal(300000),
        iso_bargain_element=Decimal(200000),
        amt_adjustments=Decimal(200000),
    )
    r = compute(ret)
    # Just verify AMT engages (>0) — exact value depends on bracket walk.
    assert r.amt > 0


def test_pre_tcja_ltcg_zero_pct_for_low_income():
    """TY2016 MFJ with $50k LTCG only (no wages): TI = $50k − $12,600 std − 2×$4,050 PE
    = $29,300, which is under $75,300 → 0% LTCG rate."""
    ret = Return(
        tax_year=2016, filing_status=FilingStatus.MFJ,
        long_term_capital_gains=Decimal(50000),
    )
    r = compute(ret)
    assert r.qualified_tax == Decimal("0.00"), f"LTCG should be 0% here, got {r.qualified_tax}"


def test_se_wage_base_2018_vs_2024():
    """Social Security wage base climbed from $128,400 (2018) to $168,600 (2024).
    A $200k SE-income filer should owe more SS-portion SE tax in 2024."""
    base_se = dict(filing_status=FilingStatus.SINGLE, se_income=Decimal(200000))
    r18 = compute(Return(tax_year=2018, **base_se))
    r24 = compute(Return(tax_year=2024, **base_se))
    # SS portion = wage_base × 0.9235 × 0.124. Difference ≈ ($168.6k − $128.4k)×0.9235×0.124 = ~$4,605
    assert r24.se_tax > r18.se_tax + Decimal(4000)


def test_all_8_historical_years_load_cleanly():
    """Smoke test: every historical year should produce a valid result for a
    moderate filer with mixed income."""
    for year in [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022]:
        ret = Return(
            tax_year=year, filing_status=FilingStatus.MFJ,
            wages=Decimal(120000),
            interest_income=Decimal(500),
            ordinary_dividends=Decimal(2000),
            qualified_dividends=Decimal(1500),
            long_term_capital_gains=Decimal(5000),
            qualifying_children=1,
        )
        r = compute(ret)
        assert r.total_tax > 0, f"TY{year} should produce nonzero tax"
        assert r.agi == Decimal("127500.00"), f"TY{year} AGI"
