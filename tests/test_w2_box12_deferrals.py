"""Unit tests for the W-2 Box 12 elective-deferral parser.

These verify that 401(k) and Roth 401(k) contributions get recovered from
bundled W-2 text inside a multi-form PDF. The 1040 itself doesn't show
elective deferrals (they're already excluded from Box 1 wages), so the
only path to capture them when text-extracting is the W-2 form.

All fixtures are synthetic, no PII.
"""
from decimal import Decimal

from taxlens.importers.pdf import _extract_w2_box12_deferrals


def test_no_w2_fingerprint_returns_empty():
    # 1040-only text without any W-2 markers should not match anything,
    # even if it happens to contain letter+number patterns.
    text = "Form 1040 line 1a Wages 100000\nAdjusted gross income 11 95000"
    assert _extract_w2_box12_deferrals(text) == {}


def test_traditional_401k_code_d():
    text = """
    Form W-2 Wage and Tax Statement
    Box 1 Wages tips other 80000.00
    Box 12a D 19500.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["traditional_401k_contributions"] == Decimal("19500.00")
    assert "roth_401k_contributions" not in out


def test_roth_401k_code_aa():
    text = """
    Form W-2 Wage and Tax Statement
    Box 12a AA 7500.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["roth_401k_contributions"] == Decimal("7500.00")


def test_both_codes_on_same_w2():
    text = """
    Form W-2 Wage and Tax Statement
    12a D 10000.00
    12b AA 5000.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["traditional_401k_contributions"] == Decimal("10000.00")
    assert out["roth_401k_contributions"] == Decimal("5000.00")


def test_multiple_w2s_sum():
    # Joint return with two employers — both contributions should sum.
    text = """
    Form W-2 Wage and Tax Statement
    12a D 12000.00
    Form W-2 Wage and Tax Statement
    12a D 8000.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["traditional_401k_contributions"] == Decimal("20000.00")


def test_column_layout_amount_on_next_line():
    # Some vendor PDFs render the Box 12 column with the code on one row
    # and the amount in the value column.
    text = """
    Form W-2
    Box 12
    D 19,500.00
    AA 5,000.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["traditional_401k_contributions"] == Decimal("19500.00")
    assert out["roth_401k_contributions"] == Decimal("5000.00")


def test_403b_roth_code_bb_buckets_into_roth():
    text = """
    Form W-2
    12a BB 6000.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["roth_401k_contributions"] == Decimal("6000.00")


def test_govt_457b_roth_code_ee_buckets_into_roth():
    text = """
    Form W-2
    12a EE 4000.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out["roth_401k_contributions"] == Decimal("4000.00")


def test_ignores_unrelated_codes():
    # Box 12 has many codes that aren't elective deferrals
    # (e.g. C = group-term life > 50k, DD = employer health cost).
    # Those should NOT be summed into either bucket.
    text = """
    Form W-2
    12a C 250.00
    12b DD 15000.00
    """
    out = _extract_w2_box12_deferrals(text)
    assert out == {}


def test_address_collisions_not_picked_up():
    # Capital-letter sequences in addresses or names must not be parsed
    # as Box-12 codes when no W-2 / Box 12 marker is present.
    text = "John Doe, AA 123 Main St 1234.00"
    assert _extract_w2_box12_deferrals(text) == {}
