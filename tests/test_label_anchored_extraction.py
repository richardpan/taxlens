"""Year-resilient label-anchored extraction.

Verifies that the 1040 importer's headline reporting fields
(``agi_reported``, ``taxable_income_reported``, ``total_tax_reported``,
``deduction_reported``, ``federal_withholding``,
``child_tax_credit_reported``, ``schedule_2_other_taxes_reported``,
etc.) recover the right values regardless of which 1040 line each
field lives on. Form 1040 line numbers shift across major-version
years (TY2018 postcard, TY2019 layout, TY2020+ current, TY2025+ OBBB
restructure, pre-TCJA TY2017 long-form), but the verbatim *labels*
are stable. Extraction must follow the labels, not the line numbers.

These synthetic fixtures intentionally do NOT use any real-return
data — they reproduce only the structure of the public IRS forms so
we can assert on extraction without leaking any dollar amounts from
actual filings.
"""
from decimal import Decimal

import pytest

from taxlens.importers.pdf import _extract_fields


TY2019_TEXT = """\
Form 1040 (2019)            U.S. Individual Income Tax Return       2019      OMB No. 1545-0074
Department of the Treasury — Internal Revenue Service
Filing Status:  Single

  1   Wages, salaries, tips, etc. Attach Form(s) W-2 .............. 1     85,000
  2 a Tax-exempt interest ........... 2a       0  b Taxable interest ............. 2b    400
  3 a Qualified dividends ........... 3a   1,200  b Ordinary dividends ............ 3b  2,500
  4 a IRA distributions ............. 4a       0  b Taxable amount ................ 4b      0
  5 a Pensions and annuities ........ 5a       0  b Taxable amount ................ 5b      0
  6 a Social security benefits ...... 6a       0  b Taxable amount ................ 6b      0
  7 a Other gains or (losses) ....... 7a       0  b Capital gain or (loss). Attach Schedule D     7b   3,000
  8 a Other income from Schedule 1, line 9 ......................... 8a       0
    b Add lines 1, 2b, 3b, 4b, 5b, 6b, 7b, and 8a. This is your total income      9   92,100
  9   Adjustments to income from Schedule 1, line 22 .............. 10        0
 10   Subtract line 10 from line 9. This is your adjusted gross income          8b  92,100
 11   Standard deduction or itemized deductions (from Schedule A) ..  9    12,200
 12   Qualified business income deduction. Attach Form 8995 ........ 10        0
 13 a Add lines 9 and 10 ............................. 11a   12,200
    b Subtract line 11a from line 8b. This is your taxable income .. 11b   79,900
 14 a Tax (see inst.) Check if any from: ............ 12a   13,323
    b Add Schedule 2, line 3 to line 12a, total ...... 12b   13,323
 15 a Child tax credit or credit for other dependents 13a    2,000
    b Add Schedule 3, line 7 to line 13a ............. 13b    2,000
 16   Subtract line 13b from line 12b ............................ 14    11,323
 17   Other taxes. Attach Schedule 2 .............................. 15       500
 18   Add lines 14 and 15. This is your total tax ................. 16    11,823
 19   Federal income tax withheld from Forms W-2 and 1099 ......... 17    14,000
 20   Refundable credits ........................................ 18a       0
"""


TY2018_TEXT = """\
Form 1040 (2018)        U.S. Individual Income Tax Return  2018  OMB No. 1545-0074
Department of the Treasury — Internal Revenue Service
Filing Status:  Single

  1  Wages, salaries, tips, etc. Attach Form(s) W-2 .............  1   85,000
  2 a Tax-exempt interest ......... 2a    0  b Taxable interest .... 2b   400
  3 a Qualified dividends ......... 3a 1,200  b Ordinary dividends .. 3b 2,500
  4 a IRAs, pensions, annuities ... 4a    0  b Taxable amount ....... 4b     0
  5 a Social security benefits .... 5a    0  b Taxable amount ....... 5b     0
  6  Total income. Add lines 1 through 5 ............................. 6  89,100
  7  Adjusted gross income .......................................... 7  89,100
  8  Standard deduction or itemized deductions (from Schedule A) ...  8  12,000
  9  Qualified business income deduction. Attach Form 8995 ......... 9       0
 10  Taxable income. Subtract lines 8 and 9 from line 7 ............ 10  77,100
 11  Tax (see inst.) ........... 11    13,000
 12 a Child tax credit / Credit for other dependents .............. 12a  2,000
    b Add Schedule 3 nonrefundable credits ........................ 12b  2,000
 13  Subtract line 12 from line 11 ................................. 13  11,000
 14  Other taxes. Attach Schedule 4 ................................ 14    500
 15  Total tax. Add lines 13 and 14 ................................ 15  11,500
 16  Federal income tax withheld from Forms W-2 and 1099 .......... 16  14,000
 17 a Earned income credit (EIC) ................................. 17a    0
    b Additional child tax credit. Attach Schedule 8812 .......... 17b    0
"""


TY2017_TEXT = """\
Form 1040 (2017)  U.S. Individual Income Tax Return  2017   OMB No. 1545-0074
Department of the Treasury — Internal Revenue Service
Filing Status:  Single

  7  Wages, salaries, tips, etc. Attach Form(s) W-2 .............. 7   85,000
  8 a Taxable interest ............................................ 8a    400
  9 a Ordinary dividends .......................................... 9a   2,500
    b Qualified dividends ......................................... 9b   1,200
 13  Capital gain or (loss). Attach Schedule D if required ......  13   3,000
 22  Total income. Add the amounts in the far right column ....... 22  90,900
 36  Total adjustments .......................................... 36       0
 37  Adjusted gross income ...................................... 37  90,900
 38  Amount from line 37 (adjusted gross income) ................ 38  90,900
 40  Itemized deductions or standard deduction .................. 40  10,400
 42  Personal exemptions ........................................ 42   4,050
 43  Taxable income. Subtract line 42 from line 41 .............. 43  76,450
 44  Tax (see instructions) ..................................... 44  14,800
 52  Child tax credit and credit for other dependents ........... 52   1,000
 56  Subtract total credits from tax ............................ 56  13,800
 62  Other taxes ................................................ 62     500
 63  Total tax. Add lines 56 through 62 ......................... 63  14,300
 64  Federal income tax withheld from Forms W-2 and 1099 ........ 64  16,000
 65  2017 estimated tax payments ................................ 65       0
"""


CASES = [
    ("TY2019", TY2019_TEXT, {
        "wages": Decimal("85000.00"),
        "interest_income": Decimal("400.00"),
        "qualified_dividends": Decimal("1200.00"),
        "ordinary_dividends": Decimal("2500.00"),
        "agi_reported": Decimal("92100.00"),
        "deduction_reported": Decimal("12200.00"),
        "taxable_income_reported": Decimal("79900.00"),
        "total_tax_reported": Decimal("11823.00"),
        "federal_withholding": Decimal("14000.00"),
        "child_tax_credit_reported": Decimal("2000.00"),
        "schedule_2_other_taxes_reported": Decimal("500.00"),
    }),
    ("TY2018", TY2018_TEXT, {
        "wages": Decimal("85000.00"),
        "interest_income": Decimal("400.00"),
        "qualified_dividends": Decimal("1200.00"),
        "ordinary_dividends": Decimal("2500.00"),
        "agi_reported": Decimal("89100.00"),
        "deduction_reported": Decimal("12000.00"),
        "taxable_income_reported": Decimal("77100.00"),
        "total_tax_reported": Decimal("11500.00"),
        "federal_withholding": Decimal("14000.00"),
        "child_tax_credit_reported": Decimal("2000.00"),
    }),
    ("TY2017", TY2017_TEXT, {
        "wages": Decimal("85000.00"),
        "interest_income": Decimal("400.00"),
        "qualified_dividends": Decimal("1200.00"),
        "ordinary_dividends": Decimal("2500.00"),
        "agi_reported": Decimal("90900.00"),
        "deduction_reported": Decimal("10400.00"),
        "taxable_income_reported": Decimal("76450.00"),
        "total_tax_reported": Decimal("14300.00"),
        "federal_withholding": Decimal("16000.00"),
    }),
]


@pytest.mark.parametrize("name,text,expected", CASES, ids=[c[0] for c in CASES])
def test_label_anchored_extraction_pre_2020(name: str, text: str, expected: dict) -> None:
    year = int(name.replace("TY", ""))
    fields, _children, _warnings = _extract_fields([text], tax_year=year)
    misses = [k for k in expected if k not in fields]
    wrongs = [(k, expected[k], fields[k]) for k in expected
              if k in fields and fields[k] != expected[k]]
    assert not misses, f"{name} missed labels: {misses}"
    assert not wrongs, f"{name} wrong values: {wrongs}"


def test_pre2020_supplement_not_applied_for_modern_years() -> None:
    """The pre-2020 supplemental patterns must NOT fire when the
    detected tax year is >= 2020. The same label phrases that uniquely
    anchor the right line on TY2018/2019 layouts can over-match on
    TY2020+ forms (where the same phrasing also appears in
    cross-reference text, the Standard Deduction sidebar, Schedule
    8812 itself, etc.). Gating on year keeps modern-form extraction
    bit-for-bit identical to the pre-supplement behavior — proven
    here by running a TY2019 layout under year=2023 and confirming
    the supplement does not contribute any of its fields.
    """
    fields, _ch, _ws = _extract_fields([TY2019_TEXT], tax_year=2023)
    # The TY2019 layout has 'deduction' / 'CTC' / 'other taxes' on
    # lines that the legacy patterns don't anchor — those fields
    # should be ABSENT under year=2023, demonstrating the supplement
    # was not applied.
    assert "deduction_reported" not in fields
    assert "child_tax_credit_reported" not in fields
    assert "schedule_2_other_taxes_reported" not in fields
