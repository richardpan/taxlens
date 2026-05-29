"""IRA deductibility phaseout (§219(g)) for active workplace-plan participants."""
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


# ── No workplace plan → full deduction (subject to limit) ─────────────────────

def test_full_deduction_when_not_covered() -> None:
    r = _ret(traditional_ira_contributions=Decimal(7_000))
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal("7000.00")
    assert result.ira_deduction_disallowed == Decimal(0)
    # AGI reduced by full $7000
    assert result.agi == Decimal("43000.00")


def test_contribution_capped_at_limit() -> None:
    """Even uncovered filer is limited to the §219(b) annual cap ($7k under-50 in 2024)."""
    r = _ret(traditional_ira_contributions=Decimal(10_000))
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal("7000.00")


def test_catch_up_limit_for_50_plus() -> None:
    r = _ret(
        wages=Decimal(50_000),
        traditional_ira_contributions=Decimal(10_000),
        taxpayer_age=55,
    )
    result = compute(r, load_rules(2024))
    # 50+ catch-up limit is $8000 in 2024
    assert result.ira_deduction_allowed == Decimal("8000.00")


# ── Active participant phaseout ──────────────────────────────────────────────

def test_full_deduction_when_covered_but_below_phaseout() -> None:
    r = _ret(
        wages=Decimal(70_000),
        traditional_ira_contributions=Decimal(7_000),
        is_covered_by_workplace_plan=True,
        # 2024 single phaseout: $77k - $87k. Wages $70k → MAGI $70k < $77k.
    )
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal("7000.00")


def test_no_deduction_when_covered_and_above_phaseout() -> None:
    r = _ret(
        wages=Decimal(100_000),
        traditional_ira_contributions=Decimal(7_000),
        is_covered_by_workplace_plan=True,
        # MAGI $100k > $87k end → fully phased out.
    )
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal(0)
    assert result.ira_deduction_disallowed == Decimal("7000.00")
    # AGI is unchanged by IRA contribution
    assert result.agi == Decimal("100000.00")


def test_partial_deduction_in_phaseout_range() -> None:
    """Single, MAGI $82k → halfway through $77k-$87k window."""
    r = _ret(
        wages=Decimal(82_000),
        traditional_ira_contributions=Decimal(7_000),
        is_covered_by_workplace_plan=True,
    )
    result = compute(r, load_rules(2024))
    # ratio = (87000 - 82000) / (87000 - 77000) = 0.5
    # allowed = 7000 * 0.5 = 3500
    assert result.ira_deduction_allowed == Decimal("3500.00")
    assert result.ira_deduction_disallowed == Decimal("3500.00")


def test_mfj_higher_phaseout_thresholds() -> None:
    """MFJ covered: $123k - $143k in 2024."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(133_000),
        traditional_ira_contributions=Decimal(7_000),
        is_covered_by_workplace_plan=True,
    )
    result = compute(r, load_rules(2024))
    # Midpoint → 50% allowed
    assert result.ira_deduction_allowed == Decimal("3500.00")


def test_spouse_covered_only_uses_higher_phaseout() -> None:
    """MFJ where only spouse is covered: $230k - $240k window."""
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(150_000),
        traditional_ira_contributions=Decimal(7_000),
        is_covered_by_workplace_plan=False,
        spouse_covered_by_workplace_plan=True,
        # MAGI $150k < $230k start → full deduction
    )
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal("7000.00")


def test_spouse_covered_only_phases_out_above_240k() -> None:
    r = _ret(
        filing_status=FilingStatus.MFJ,
        wages=Decimal(250_000),
        traditional_ira_contributions=Decimal(7_000),
        is_covered_by_workplace_plan=False,
        spouse_covered_by_workplace_plan=True,
    )
    result = compute(r, load_rules(2024))
    assert result.ira_deduction_allowed == Decimal(0)


# ── Historical accuracy ──────────────────────────────────────────────────────

def test_2019_uses_old_6000_limit() -> None:
    r = _ret(
        tax_year=2019,
        wages=Decimal(50_000),
        traditional_ira_contributions=Decimal(10_000),
    )
    result = compute(r, load_rules(2019))
    assert result.ira_deduction_allowed == Decimal("6000.00")


def test_2015_uses_old_5500_limit() -> None:
    r = _ret(
        tax_year=2015,
        wages=Decimal(50_000),
        traditional_ira_contributions=Decimal(10_000),
    )
    result = compute(r, load_rules(2015))
    assert result.ira_deduction_allowed == Decimal("5500.00")
