"""Property-based tests with Hypothesis.

These hammer the engine + bracket walker with random inputs to catch
edge cases that hand-written examples miss (rounding, off-by-one in
bracket boundaries, monotonicity violations under what-ifs).
"""
from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from taxlens import compute
from taxlens.brackets import walk_brackets
from taxlens.models import FilingStatus, Return
from taxlens.rules import load_rules


CENT = Decimal("0.01")

money_st = st.decimals(
    min_value=Decimal(0), max_value=Decimal(5_000_000),
    allow_nan=False, allow_infinity=False, places=2,
)
small_money_st = st.decimals(
    min_value=Decimal(0), max_value=Decimal(50_000),
    allow_nan=False, allow_infinity=False, places=2,
)


# ─── bracket walker ────────────────────────────────────────────────────────

@given(money_st)
@settings(max_examples=200, deadline=None)
def test_bracket_walker_sums_match_total_tax(amount):
    """Σ(fill.tax_in_bracket) must equal the returned tax (within a cent)."""
    rules = load_rules(2024)
    brackets = rules.ordinary_brackets["single"]
    total, fills = walk_brackets(amount, brackets)
    summed = sum((f.tax_in_bracket for f in fills), Decimal(0))
    assert abs(total - summed) <= CENT


@given(money_st, money_st)
@settings(max_examples=100, deadline=None)
def test_bracket_walker_monotonic(a, b):
    """More taxable income ⇒ at least as much ordinary tax (never less)."""
    if a > b:
        a, b = b, a
    rules = load_rules(2024)
    brackets = rules.ordinary_brackets["mfj"]
    ta, _ = walk_brackets(a, brackets)
    tb, _ = walk_brackets(b, brackets)
    assert tb + CENT >= ta


# ─── engine ────────────────────────────────────────────────────────────────

@given(
    wages=st.decimals(min_value=Decimal(0), max_value=Decimal(1_000_000), places=2),
    interest=small_money_st,
    ltcg=small_money_st,
    qd=small_money_st,
)
@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_total_tax_nonnegative(wages, interest, ltcg, qd):
    """No combination of legal inputs should produce a negative federal tax."""
    qd = min(qd, ltcg)  # qualified divs ≤ ordinary divs by IRS definition; here ≤ ltcg as proxy
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.MFJ,
        wages=wages, interest_income=interest,
        long_term_capital_gains=ltcg, qualified_dividends=qd,
    )
    r = compute(ret)
    assert r.total_tax >= 0
    assert r.ordinary_tax >= 0
    assert r.qualified_tax >= 0


@given(
    wages=st.decimals(min_value=Decimal(50_000), max_value=Decimal(400_000), places=2),
    bump=st.decimals(min_value=Decimal(0), max_value=Decimal(50_000), places=2),
)
@settings(max_examples=60, deadline=None)
def test_more_wages_means_at_least_as_much_tax(wages, bump):
    """Monotonicity at the engine level: adding wages can't reduce total tax."""
    base = Return(tax_year=2024, filing_status=FilingStatus.SINGLE, wages=wages)
    bumped = Return(tax_year=2024, filing_status=FilingStatus.SINGLE, wages=wages + bump)
    r1 = compute(base)
    r2 = compute(bumped)
    assert r2.total_tax + CENT >= r1.total_tax


@given(
    wages=st.decimals(min_value=Decimal(50_000), max_value=Decimal(300_000), places=2),
    coll=st.decimals(min_value=Decimal(0), max_value=Decimal(100_000), places=2),
)
@settings(max_examples=50, deadline=None)
def test_collectibles_never_exceed_28_percent(wages, coll):
    """The collectibles rate cap must be respected for any input combination."""
    ret = Return(
        tax_year=2024, filing_status=FilingStatus.SINGLE,
        wages=wages, collectibles_gains=coll,
    )
    r = compute(ret)
    if coll > 0:
        # Effective rate on the collectibles bucket can't exceed 28% (+ cent rounding).
        assert r.collectibles_tax <= coll * Decimal("0.28") + CENT
