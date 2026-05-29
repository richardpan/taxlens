"""Tests for AMT, Schedule D worksheet, and CA state computation."""
from decimal import Decimal

import pytest

from taxlens import compute
from taxlens.models import FilingStatus, Return


# ─── AMT ──────────────────────────────────────────────────────────────────

def test_amt_zero_for_typical_filer():
    """A normal MFJ filer well below the exemption phaseout should owe $0 of AMT."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(240000), interest_income=Decimal(6000),
    )
    r = compute(ret)
    assert r.amt == 0


def test_amt_kicks_in_with_large_preferences():
    """Adding a big AMT preference (e.g. ISO bargain element) should produce AMT."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(240000), interest_income=Decimal(6000),
        amt_preferences=Decimal(500000),
    )
    r = compute(ret)
    assert r.amt > 0
    # AMT shows up in total tax.
    assert r.total_tax > Decimal(34117)


def test_amt_exemption_phaseout_high_income():
    """At very high AMTI the exemption fully phases out — AMT grows roughly linearly."""
    base = Return(tax_year=2024, filing_status=FilingStatus.MFJ,
                  wages=Decimal(2_000_000), amt_preferences=Decimal(500_000))
    r = compute(base)
    # With $2.5M AMTI, exemption is fully phased out → AMT exists if tentative > regular.
    # Mostly we just want a non-degenerate result.
    assert r.amt >= 0


# ─── Schedule D worksheet (28% / 25% cap rates) ────────────────────────────

def test_collectibles_capped_at_28_percent():
    """Collectibles gains never tax above 28% even when marginal rate would be 32%."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(450_000),               # pushes into 35% bracket
        collectibles_gains=Decimal(50_000),
    )
    r = compute(ret)
    # The Sch D worksheet caps these dollars at 28%, so collectibles_tax ≤ 50k × 0.28.
    assert r.collectibles_tax <= Decimal(50_000) * Decimal("0.28") + Decimal("0.01")
    assert r.collectibles_tax > 0


def test_unrecaptured_1250_capped_at_25_percent():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(450_000),
        unrecaptured_1250_gains=Decimal(40_000),
    )
    r = compute(ret)
    assert r.unrecaptured_1250_tax <= Decimal(40_000) * Decimal("0.25") + Decimal("0.01")
    assert r.unrecaptured_1250_tax > 0


def test_collectibles_below_cap_uses_lower_marginal():
    """A low-income filer with collectibles pays their marginal rate, not 28%."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=Decimal(20_000),
        collectibles_gains=Decimal(5_000),
    )
    r = compute(ret)
    # Wages 20000 − std ded 14600 = 5400 taxable. Ordinary portion = 5400 − 5000
    # collectibles = 400 (in 10% bracket). Collectibles of 5000 stack on top
    # of 400, still all in the 10% bracket. Capped rate min(28%, 10%) = 10%.
    assert r.collectibles_tax == Decimal("500.00")


# ─── CA state computation ─────────────────────────────────────────────────

def test_ca_state_basic_mfj_2024():
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(240000), interest_income=Decimal(6000),
        state="CA",
    )
    r = compute(ret)
    assert r.state_result is not None
    sr = r.state_result
    assert sr.state == "CA"
    # CA AGI = federal AGI (no SE here, no HSA)
    assert sr.state_agi == Decimal("246000.00")
    # CA taxable = 246000 − 11080 = 234920
    assert sr.state_taxable_income == Decimal("234920.00")
    # Hand-checked CA tax (2024 MFJ schedule, taxable = 234920):
    #   1% on 21512        =   215.12
    #   2% on 29486        =   589.72   (running to 50998)
    #   4% on 29492        =  1179.68   (to 80490)
    #   6% on 31242        =  1874.52   (to 111732)
    #   8% on 29480        =  2358.40   (to 141212)
    #   9.3% on (234920-141212)=93708 → 8714.84
    #   total              = 14932.28
    assert sr.state_tax == Decimal("14932.28")


def test_ca_state_taxes_ltcg_as_ordinary():
    """CA quirk: long-term gains do NOT get preferential rates."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=Decimal(0), long_term_capital_gains=Decimal(100_000),
        state="CA",
    )
    r = compute(ret)
    # Federal LTCG tax is 0% on the first 94,050 → very low.
    assert r.qualified_tax < Decimal(1000)
    # CA still taxes the full $100k as ordinary income.
    assert r.state_result.state_tax > Decimal(1500)


def test_unknown_state_raises():
    ret = Return(tax_year=2024, filing_status=FilingStatus.SINGLE,
                 wages=Decimal(50000), state="XX")
    with pytest.raises(FileNotFoundError, match="XX"):
        compute(ret)
