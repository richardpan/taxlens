"""Unit tests for the AcroForm tooltip → TaxLens-field classifier.

These do NOT require reportlab (unlike test_third_party_pdfs.py, which
builds real PDFs); they directly drive _classify_tooltip with the exact
tooltip strings the IRS fillable 1040 PDFs use. This is the fast loop
for adding/maintaining patterns when users report unmapped fields.

The complaint that motivated this file: a real PDF imported via v0.34.0
left wages ($0) missing even though line 1a was populated. The actual
IRS line 1a tooltip text reads "Total amount from Form(s) W-2, box 1
(see instructions)" — note no "1a" prefix and no "Wages" word — and
all the prior patterns required one or the other.
"""
from taxlens.importers.acroform import _classify_tooltip


def test_irs_line_1a_modern_tooltip_maps_to_wages():
    """TY2022+ 1040 line 1a — this was the regression."""
    tooltip = "Total amount from Form(s) W-2, box 1 (see instructions)"
    assert _classify_tooltip(tooltip) == "wages"


def test_irs_line_1z_total_maps_to_wages():
    """TY2022+ line 1z aggregates 1a–1h. Should also resolve to wages
    so the conflict resolver picks the larger (i.e. the total) when
    both are present."""
    tooltip = "Add lines 1a through 1h"
    assert _classify_tooltip(tooltip) == "wages"


def test_legacy_wages_tooltip_still_works():
    """Pre-2022 1040 line 1 single-line wording must keep matching."""
    tooltip = "Wages, salaries, tips, etc. Attach Form(s) W-2"
    assert _classify_tooltip(tooltip) == "wages"


def test_irs_line_2b_interest_tooltip():
    assert _classify_tooltip("Taxable interest") == "interest_income"


def test_irs_line_3a_qual_div_tooltip():
    assert _classify_tooltip("Qualified dividends") == "qualified_dividends"


def test_irs_line_3b_ord_div_tooltip():
    assert _classify_tooltip("Ordinary dividends") == "ordinary_dividends"


def test_irs_line_4b_ira_taxable_tooltip():
    """Line 4b's tooltip is typically 'IRA distributions ... Taxable amount'."""
    tip = "IRA distributions. Taxable amount"
    assert _classify_tooltip(tip) == "ira_distributions_taxable"


def test_irs_line_5b_pension_taxable_tooltip():
    tip = "Pensions and annuities. Taxable amount"
    assert _classify_tooltip(tip) == "pension_distributions_taxable"


def test_irs_line_7_capital_gain_tooltip():
    """Line 7 tooltip omits 'long-term' — it says 'Capital gain or (loss)'.
    We map any 1040 line 7 amount to long_term_capital_gains because that's
    where Schedule D flows on the 1040 form."""
    tip = "Capital gain or (loss). Attach Schedule D if required."
    assert _classify_tooltip(tip) == "long_term_capital_gains"


def test_irs_line_11_agi_tooltip():
    assert _classify_tooltip("Adjusted gross income") == "agi_reported"


def test_irs_line_15_taxable_income_tooltip():
    assert _classify_tooltip("Taxable income") == "taxable_income_reported"


def test_irs_line_24_total_tax_tooltip():
    """Line 24 actually reads 'Add lines 22 and 23. This is your total tax'."""
    tip = "Add lines 22 and 23. This is your total tax"
    assert _classify_tooltip(tip) == "total_tax_reported"


def test_irs_line_25a_withholding_tooltip():
    tip = "Federal income tax withheld from Form(s) W-2"
    assert _classify_tooltip(tip) == "federal_withholding"


def test_unrelated_tooltip_returns_none():
    """Negative case — a non-money label must not accidentally classify."""
    assert _classify_tooltip("Your first name and middle initial") is None
    assert _classify_tooltip("Spouse's social security number") is None
    assert _classify_tooltip("") is None
