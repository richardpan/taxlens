"""Phase 2 federal coverage: unemployment + student loan interest + educator expenses."""
from decimal import Decimal

from taxlens.engine import compute
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


def _ret(**kw) -> Return:
    base = dict(
        tax_year=2024,
        filing_status=FilingStatus.SINGLE,
        wages=Decimal(50_000),
        federal_withholding=Decimal(0),
    )
    base.update(kw)
    return Return(**base)


# ── Unemployment compensation ────────────────────────────────────────────────

def test_unemployment_flows_into_agi() -> None:
    r = _ret(unemployment_compensation=Decimal(10_000))
    result = compute(r, load_rules(2024))
    assert result.agi == Decimal("60000.00")


# ── Student loan interest (§221) ─────────────────────────────────────────────

def test_sli_full_deduction_when_below_phaseout() -> None:
    r = _ret(
        wages=Decimal(50_000),
        student_loan_interest_paid=Decimal(1_500),
        # 2024 single phaseout: $80k-$95k. MAGI $50k → below start → full.
    )
    result = compute(r, load_rules(2024))
    assert result.student_loan_interest_deduction == Decimal("1500.00")
    assert result.agi == Decimal("48500.00")


def test_sli_capped_at_2500() -> None:
    r = _ret(
        wages=Decimal(50_000),
        student_loan_interest_paid=Decimal(5_000),
    )
    result = compute(r, load_rules(2024))
    assert result.student_loan_interest_deduction == Decimal("2500.00")


def test_sli_partial_in_phaseout() -> None:
    """Single MAGI $87.5k → halfway through $80k–$95k window."""
    r = _ret(
        wages=Decimal(87_500),
        student_loan_interest_paid=Decimal(2_500),
    )
    result = compute(r, load_rules(2024))
    # ratio = (95000-87500)/(95000-80000) = 0.5; allowed = 2500 * 0.5 = 1250
    assert result.student_loan_interest_deduction == Decimal("1250.00")


def test_sli_zero_above_phaseout() -> None:
    r = _ret(
        wages=Decimal(100_000),
        student_loan_interest_paid=Decimal(2_500),
    )
    result = compute(r, load_rules(2024))
    assert result.student_loan_interest_deduction == Decimal(0)


def test_sli_disabled_for_mfs() -> None:
    """IRC §221(e)(2): MFS can't claim SLI at all."""
    r = _ret(
        filing_status=FilingStatus.MFS,
        wages=Decimal(40_000),
        student_loan_interest_paid=Decimal(2_500),
    )
    result = compute(r, load_rules(2024))
    assert result.student_loan_interest_deduction == Decimal(0)


def test_sli_mfj_higher_phaseout() -> None:
    """MFJ 2024 phaseout: $165k-$195k."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(150_000),
        student_loan_interest_paid=Decimal(2_500),
    )
    result = compute(r, load_rules(2024))
    # Below MFJ start → full $2500
    assert result.student_loan_interest_deduction == Decimal("2500.00")


# ── Educator expenses (§62(a)(2)(D)) ─────────────────────────────────────────

def test_educator_full_when_below_cap() -> None:
    r = _ret(educator_expenses=Decimal(200))
    result = compute(r, load_rules(2024))
    assert result.educator_expense_deduction == Decimal("200.00")


def test_educator_capped_at_300_in_2024() -> None:
    r = _ret(educator_expenses=Decimal(500))
    result = compute(r, load_rules(2024))
    assert result.educator_expense_deduction == Decimal("300.00")


def test_educator_cap_was_250_pre_2022() -> None:
    r = _ret(tax_year=2021, educator_expenses=Decimal(500))
    result = compute(r, load_rules(2021))
    assert result.educator_expense_deduction == Decimal("250.00")


def test_educator_doubled_for_mfj() -> None:
    """MFJ allows up to 2× per-educator cap so both spouses can claim."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        educator_expenses=Decimal(600),
    )
    result = compute(r, load_rules(2024))
    # 2 × $300 = $600 cap
    assert result.educator_expense_deduction == Decimal("600.00")


def test_educator_mfj_over_doubled_cap() -> None:
    r = _ret(
        filing_status=FilingStatus.MFJ,
        educator_expenses=Decimal(1_000),
    )
    result = compute(r, load_rules(2024))
    assert result.educator_expense_deduction == Decimal("600.00")


# ── Combined sanity ─────────────────────────────────────────────────────────

def test_combined_adjustments_reduce_agi() -> None:
    r = _ret(
        wages=Decimal(60_000),
        unemployment_compensation=Decimal(5_000),
        student_loan_interest_paid=Decimal(1_000),
        educator_expenses=Decimal(300),
    )
    result = compute(r, load_rules(2024))
    # gross = 60k + 5k = 65k
    # adjustments = 1000 SLI + 300 educator = 1300
    # agi = 65000 - 1300 = 63700
    assert result.agi == Decimal("63700.00")
