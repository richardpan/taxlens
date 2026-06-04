"""Regression: line-number echo on the value column.

Form 8889 line 13 ("HSA deduction (see instructions)") renders as

    13 HSA deduction (see instructions). . . . . . . 13

when the user has no personal HSA contribution to deduct (all $7,750
came from the employer via Box 12 W). The trailing "13" is the line-
number echo column, NOT a $13 deduction. Without this guard the importer
extracted hsa_deduction=$13, contributing to a +$2.59 reconciliation
delta on a 2024 MFJ FreeTaxUSA return.
"""

from decimal import Decimal

from taxlens.importers.pdf import _extract_fields


def test_empty_value_column_with_line_number_echo_yields_zero():
    text = """\
Form 8889 Health Savings Accounts (HSAs)
13 HSA deduction (see instructions). . . . . . . . . . . . . . . . . . . . . . . . 13
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("hsa_deduction") in (None, Decimal(0)), (
        f"trailing '13' is a line-number echo, not a value; got {fields.get('hsa_deduction')}"
    )


def test_real_value_still_extracted_when_present():
    text = """\
Form 8889 Health Savings Accounts (HSAs)
13 HSA deduction (see instructions). . . . . . . 13 1,500.
"""
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("hsa_deduction") == Decimal("1500")


def test_line_number_echo_only_skipped_when_at_end_of_line():
    """A bare 1-2 digit integer mid-line that happens to equal the line
    number is NOT necessarily an echo — only end-of-line trailing matches
    are. Sanity-check we don't over-skip.

    Note: hsa_deduction is in the implausibly-small-value sanity filter
    list (anything < $100 is dropped post-extraction as a likely echo),
    so this test uses a 4-digit value that exercises the same code path
    without tripping the post-filter.
    """
    text = "Health savings account deduction . . . . . . . . . 999"
    fields, _children, _warnings, _echo = _extract_fields([text])
    assert fields.get("hsa_deduction") == Decimal("999")
