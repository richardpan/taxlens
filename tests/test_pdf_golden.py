"""Golden-fixture PDF round-trip tests using realistic multi-page 1040 layout."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from taxlens import compute
from taxlens.importers.pdf import import_pdf
from tests.realistic_1040 import Realistic1040, make_realistic_1040


def test_realistic_mfj_with_dividends_and_capgains(tmp_path: Path):
    """Full multi-page 1040 with Schedule B detail and qualified income."""
    pdf = tmp_path / "ty2024_mfj_full.pdf"
    fixture = Realistic1040(
        tax_year=2024,
        filing_status_label="Married filing jointly",
        wages=Decimal("240000"),
        interest=Decimal("3500"),
        qual_div=Decimal("12000"),
        ord_div=Decimal("12000"),
        long_term_capital_gains=Decimal("25000"),
        withholding=Decimal("50000"),
        estimated=Decimal("5000"),
        total_tax_reported=Decimal("38000.00"),
        qualifying_children=2,
        interest_payers=[("Schwab Brokerage", Decimal("2500")),
                         ("Ally Savings", Decimal("1000"))],
        div_payers=[("Vanguard VTI", Decimal("8000")),
                    ("Fidelity FXAIX", Decimal("4000"))],
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.tax_year == 2024
    assert imp.ret.filing_status.value == "mfj"
    assert imp.ret.wages == Decimal("240000")
    assert imp.ret.interest_income == Decimal("3500")
    assert imp.ret.qualified_dividends == Decimal("12000")
    assert imp.ret.ordinary_dividends == Decimal("12000")
    assert imp.ret.long_term_capital_gains == Decimal("25000")
    assert imp.ret.qualifying_children == 2
    # Engine should run and produce a sensible total tax.
    res = compute(imp.ret)
    assert Decimal("30000") < res.total_tax < Decimal("60000")


def test_realistic_self_employed_with_schedule_c(tmp_path: Path):
    """SE filer with Schedule 1 + Schedule 2 (SE tax)."""
    pdf = tmp_path / "ty2024_sched_c.pdf"
    fixture = Realistic1040(
        tax_year=2024,
        filing_status_label="Single",
        wages=Decimal("0"),
        se_income=Decimal("90000"),
        withholding=Decimal("0"),
        estimated=Decimal("20000"),
        se_tax=Decimal("12717"),
        total_tax_reported=Decimal("25000.00"),
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.filing_status.value == "single"
    assert imp.ret.se_income == Decimal("90000")
    res = compute(imp.ret)
    assert res.se_tax > 0


def test_realistic_high_income_with_amt_and_ftc(tmp_path: Path):
    """High-income return with AMT (Schedule 2) + FTC (Schedule 3)."""
    pdf = tmp_path / "ty2024_amt_ftc.pdf"
    fixture = Realistic1040(
        tax_year=2024,
        filing_status_label="Married filing jointly",
        wages=Decimal("600000"),
        interest=Decimal("8000"),
        qual_div=Decimal("30000"),
        ord_div=Decimal("30000"),
        withholding=Decimal("150000"),
        amt=Decimal("8000"),
        foreign_tax_credit=Decimal("3500"),
        total_tax_reported=Decimal("160000.00"),
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.wages == Decimal("600000")
    assert imp.ret.foreign_taxes_paid == Decimal("3500")


def test_realistic_retiree_filing_jointly(tmp_path: Path):
    """Low-income retiree: minimal wages, dividend income, large std deduction."""
    pdf = tmp_path / "ty2024_retiree.pdf"
    fixture = Realistic1040(
        tax_year=2024,
        filing_status_label="Married filing jointly",
        wages=Decimal("0"),
        interest=Decimal("4500"),
        qual_div=Decimal("18000"),
        ord_div=Decimal("18000"),
        long_term_capital_gains=Decimal("12000"),
        withholding=Decimal("500"),
        estimated=Decimal("2500"),
        total_tax_reported=Decimal("0.00"),
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    res = compute(imp.ret)
    # MFJ std deduction $29,200 → most of this income falls inside the 0% LTCG bracket.
    assert res.total_tax >= Decimal("0")
    assert res.total_tax < Decimal("3000")


@pytest.mark.parametrize("status_label,enum_value", [
    ("Single", "single"),
    ("Married filing jointly", "mfj"),
    ("Married filing separately", "mfs"),
    ("Head of household", "hoh"),
    ("Qualifying surviving spouse", "qss"),
])
def test_all_filing_statuses_round_trip(tmp_path: Path, status_label, enum_value):
    pdf = tmp_path / f"ty2024_{enum_value}.pdf"
    fixture = Realistic1040(
        tax_year=2024,
        filing_status_label=status_label,
        wages=Decimal("85000"),
        withholding=Decimal("10000"),
    )
    make_realistic_1040(pdf, fixture)
    imp = import_pdf(pdf)
    assert imp.ret.filing_status.value == enum_value
    assert imp.ret.wages == Decimal("85000")
