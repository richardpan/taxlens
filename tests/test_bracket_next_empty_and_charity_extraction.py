"""Tests: walk_brackets ``include_next_empty`` flag + non-itemizer
charity importer extraction (line 10b/12b).
"""

from decimal import Decimal

from taxlens.brackets import walk_brackets
from taxlens.importers.pdf import _extract_fields


_BRACKETS_2024_MFJ = [
    (Decimal("0"), Decimal("0.10")),
    (Decimal("23200"), Decimal("0.12")),
    (Decimal("94300"), Decimal("0.22")),
    (Decimal("201050"), Decimal("0.24")),
    (Decimal("383900"), Decimal("0.32")),
    (Decimal("487450"), Decimal("0.35")),
    (Decimal("731200"), Decimal("0.37")),
]


def test_include_next_empty_appends_next_bracket_with_zero_amount():
    # Taxable income lands in the 22% bracket
    _tax, fills = walk_brackets(
        Decimal("100000"), _BRACKETS_2024_MFJ, include_next_empty=True
    )
    # Filled brackets: 10%, 12%, 22% — plus next-empty 24%.
    assert len(fills) == 4
    last = fills[-1]
    assert last.rate == Decimal("0.24")
    assert last.amount_in_bracket == Decimal(0)
    assert last.lower == Decimal("201050")
    assert last.upper == Decimal("383900")


def test_include_next_empty_off_by_default_excludes_empty():
    _tax, fills = walk_brackets(Decimal("100000"), _BRACKETS_2024_MFJ)
    # 10%, 12%, 22% — no empty next bracket.
    assert len(fills) == 3
    assert all(f.amount_in_bracket > 0 for f in fills)


def test_include_next_empty_when_already_in_top_bracket_does_not_fabricate():
    # Way above the top bracket cutoff — no "next" exists.
    _tax, fills = walk_brackets(
        Decimal("1000000"), _BRACKETS_2024_MFJ, include_next_empty=True
    )
    # Last bracket is the 37% top bracket with no upper bound.
    last = fills[-1]
    assert last.rate == Decimal("0.37")
    assert last.amount_in_bracket > 0
    assert last.upper is None


def test_include_next_empty_does_not_change_tax_total():
    tax_with, _ = walk_brackets(
        Decimal("100000"), _BRACKETS_2024_MFJ, include_next_empty=True
    )
    tax_without, _ = walk_brackets(Decimal("100000"), _BRACKETS_2024_MFJ)
    assert tax_with == tax_without


def test_non_itemizer_charity_extraction_2020_above_line():
    # 2020 1040 line 10b — CARES Act $300-cap above-the-line charity.
    text = """\
1040 (2020)
b Charitable contributions if you take the standard deduction. See instructions 10b 220
c Add lines 10a and 10b. These are your total adjustments to income . . . . 10c 220
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("charitable_contributions_non_itemizer") == Decimal("220")


def test_non_itemizer_charity_extraction_2021_below_line():
    # 2021 1040 line 12b — TCDTRA below-the-line successor.
    text = """\
1040 (2021)
b Charitable contributions if you take the standard deduction. See instructions 12b 600
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("charitable_contributions_non_itemizer") == Decimal("600")
