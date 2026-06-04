"""Regression: Form 8959 line 1 ('Medicare wages and tips from Form W-2,
box 5') must NOT be picked up as 1040 line 1a/1z wages. This was the
root cause of the +$6,080.28 reconciliation delta on a 2024 MFJ FreeTaxUSA
return where Box 5 ($243,647) was getting extracted instead of Box 1
($220,647), inflating AGI by $23k and triggering a bogus NIIT.
"""

from decimal import Decimal

from taxlens.importers.pdf import _extract_fields


def test_form_8959_medicare_wages_does_not_clobber_1040_line_1a():
    text = """\
Form 1040 U.S. Individual Income Tax Return 2024
Income 1 a Total amount from Form(s) W-2, box 1 (see instructions) . . . 1a 220,647.
   z Add lines 1a through 1h . . . . . . . . . . . . . . . . . . . 1z 220,647.
   2b Taxable interest . . . . . . . . . . . . . . . . . . . . . . 2b 4,000.

Form 8959 Additional Medicare Tax 2024
Part I Additional Medicare Tax on Medicare Wages
1 Medicare wages and tips from Form W-2, box 5. If you have more than one
Form W-2, enter the total of the amounts from box 5 . . . . . . . . 1 243,647.
4 Add lines 1 through 3 . . . . . . . . . . . . . . . . . . . 4 243,647.
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("wages") == Decimal("220647"), (
        f"expected 1040 line 1a Box-1 wages ($220,647), got {fields.get('wages')} "
        "(Form 8959 line 1 Box-5 Medicare wages must not preempt 1040 line 1a)"
    )


def test_form_8959_only_no_1040_line_does_not_match_loose_wages_pattern():
    """Even when the 1040 wage lines are missing, the loose
    `\\b1\\b...Wages` fallback must NOT match Form 8959's Medicare wages
    line — that line refers to Box 5, not Box 1."""
    text = """\
Form 8959 Additional Medicare Tax 2024
1 Medicare wages and tips from Form W-2, box 5 . . . . . . . . 1 243,647.
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("wages") is None, (
        f"Form 8959 Medicare wages must not be picked as wages, got {fields.get('wages')}"
    )


def test_loose_wages_pattern_still_matches_summary_phrasing():
    """Sanity: vendor summary `Wages, salaries, tips` phrasing still
    works after we tightened the loose `\\b1\\b...Wages` pattern."""
    text = "Wages, salaries, tips ........... 100,000"
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("wages") == Decimal("100000")
