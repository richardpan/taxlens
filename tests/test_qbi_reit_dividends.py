"""Regression: Section 199A REIT/PTP dividend QBI deduction (Form 8995 line 6).

The TaxLens engine's `_compute_qbi` previously only considered K-1
Section 199A QBI, SE income, and rental income. REIT dividend QBI
(qualified REIT dividends + PTP income, Form 8995 line 6) was missing.

For a 2024 MFJ return with no business income but $125 of REIT
dividends, this caused the engine to compute QBI deduction = $0 when
the actual return claimed $25 (= $125 × 20%), contributing to a +$5.71
reconciliation delta.
"""

from decimal import Decimal

from taxlens.engine import compute
from taxlens.importers.pdf import _extract_fields
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def test_reit_dividend_qbi_deduction_2024_mfj():
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("220647"),
        interest_income=Decimal("4000"),
        ordinary_dividends=Decimal("7029"),
        qualified_dividends=Decimal("5016"),
        long_term_capital_gains=Decimal("5569"),
        short_term_capital_gains=Decimal("4444"),
        qualified_reit_ptp_dividends=Decimal("125"),
        qualifying_children=1,
        foreign_taxes_paid=Decimal("5"),
    )
    res = compute(ret, load_rules(2024))
    # 20% of $125 REIT divs = $25 QBI deduction (no taxable-income cap
    # since taxable far exceeds REIT divs)
    assert res.qbi_deduction == Decimal("25")
    # Taxable income should match the actual return's $212,464 exactly
    assert res.taxable_income == Decimal("212464")


def test_no_qbi_deduction_when_no_qbi_inputs():
    ret = Return(
        tax_year=2024,
        filing_status=FilingStatus.MFJ,
        wages=Decimal("100000"),
    )
    res = compute(ret, load_rules(2024))
    assert res.qbi_deduction == Decimal(0)


def test_form_8995_line_6_extraction():
    text = """\
Form 8995 Qualified Business Income Deduction Simplified Computation 2024
6 Qualified REIT dividends and publicly traded partnership (PTP) income or (loss)
(see instructions) . . . . . . . . . . . . . . . . . . . . 6 125.
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("qualified_reit_ptp_dividends") == Decimal("125")
