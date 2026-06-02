"""Form 8889 extractor — pulls employer/cafeteria-plan HSA contributions
(line 9), which is authoritative even on 1040-only PDFs that don't bundle
the W-2."""

from decimal import Decimal

from taxlens.importers.pdf import _extract_form_8889


def test_no_form_8889_returns_empty():
    text = "Form 1040 U.S. Individual Income Tax Return\nWages 50,000"
    assert _extract_form_8889(text) == {}


def test_form_8889_line_9_employer_contributions():
    text = """\
Form 8889 Health Savings Accounts (HSAs)
Part I HSA Contributions
1 Coverage under an HDHP: [X] Self-only [ ] Family
2 HSA contributions you made for 2024 ........... 2 1,500.00
9 Employer contributions made to your HSAs for 2024 ........... 9 3,200.00
13 HSA deduction. Smaller of line 2 or line 12 ........... 13 1,500.00
"""
    out = _extract_form_8889(text)
    assert out.get("hsa_contributions") == Decimal("3200")
    assert out.get("_form_8889_present") is True
    assert out.get("_hsa_coverage") == "self"


def test_form_8889_line_9_zero_omitted():
    """If line 9 is zero, don't bother emitting hsa_contributions=0 — the
    Return model defaults are already zero, and downstream logic wants
    truthy values to indicate a positive signal."""
    text = """\
Form 8889 Health Savings Accounts (HSAs)
9 Employer contributions made to your HSAs for 2024 ........... 9 0
"""
    out = _extract_form_8889(text)
    assert "hsa_contributions" not in out
    assert out.get("_form_8889_present") is True


def test_form_8889_family_coverage():
    text = """\
Form 8889 Health Savings Accounts (HSAs)
1 Coverage under an HDHP: [ ] Self-only [X] Family
9 Employer contributions made to your HSAs for 2024 ........... 9 7,800.00
"""
    out = _extract_form_8889(text)
    assert out.get("_hsa_coverage") == "family"
    assert out.get("hsa_contributions") == Decimal("7800")


def test_form_8889_next_line_money_recovery():
    """Some vendors (loose layouts) put the line 9 number on the next
    visual row. _first_money_after's fallback should recover it."""
    text = """\
Form 8889 Health Savings Accounts (HSAs)
9 Employer contributions made to your HSAs for 2024
2,500.00
13 HSA deduction
"""
    out = _extract_form_8889(text)
    assert out.get("hsa_contributions") == Decimal("2500")


def test_form_8889_does_not_match_unrelated_employer_text():
    """Anchor must be specific enough to not pick up generic 'employer
    contributions' phrases on other forms."""
    text = """\
Schedule 1 Additional Income and Adjustments
Employer contributions to qualified retirement plans .... 5,000
"""
    assert _extract_form_8889(text) == {}
