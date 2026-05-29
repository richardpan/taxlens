"""Multi-year advisor rules — patterns that only emerge across returns.

Each rule takes a list of (Return, TaxResult) pairs sorted by tax_year ascending
and returns zero or more Recommendations.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from taxlens.advisor import Recommendation, _dollars, _marginal_ordinary_rate
from taxlens.models import Return, TaxResult
from taxlens.rules import load_rules

ZERO = Decimal(0)


def rule_roth_conversion_window(history: list[tuple[Return, TaxResult]]) -> list[Recommendation]:
    """Spot low-income years that are great Roth-conversion windows."""
    if len(history) < 2:
        return []
    out: list[Recommendation] = []
    incomes = [(r, res, res.taxable_income) for r, res in history]
    median = sorted(t[2] for t in incomes)[len(incomes) // 2]
    for ret, result, ti in incomes:
        if ti >= median * Decimal("0.6"):
            continue
        if ti <= 1_000:
            continue
        rules = load_rules(ret.tax_year)
        marg = _marginal_ordinary_rate(ti, rules, ret.filing_status.value)
        if marg > Decimal("0.22"):
            continue
        # Headroom to top of the 22% bracket as a Roth-conversion target.
        brackets = rules.ordinary_brackets[ret.filing_status.value]
        next_break = next((low for low, r in brackets if r > Decimal("0.22") and low > ti), None)
        if next_break is None:
            continue
        room = next_break - ti
        # Rough lifetime benefit: converting at 22% vs an assumed future 32% saves 10¢/$.
        savings = _dollars(room * Decimal("0.10"))
        out.append(Recommendation(
            id=f"roth-conv-{ret.tax_year}",
            title=f"TY {ret.tax_year} was a low-income year — Roth conversion window",
            severity="suggested",
            category="retirement",
            rationale=(
                f"Your TY {ret.tax_year} taxable income (${int(ti):,}) was well below "
                f"your multi-year median (${int(median):,}). You had room to convert "
                f"~${int(room):,} of traditional IRA/401(k) to Roth while still in the "
                f"{int(marg*100)}% bracket."
            ),
            action=f"If TY {ret.tax_year} is still amendable, evaluate a backdated conversion; otherwise apply the pattern in your next low-income year.",
            est_annual_savings=savings,
            references=["Form 8606", "Roth conversion (IRC §408A(d)(3))"],
        ))
    return out


def rule_persistent_refund(history: list[tuple[Return, TaxResult]]) -> list[Recommendation]:
    """Big refunds N years in a row = the IRS is using your money interest-free."""
    if len(history) < 2:
        return []
    refunds = [res.refund_or_owed for _, res in history[-3:]]
    if all(r > 3_000 for r in refunds):
        avg = _dollars(sum(refunds, Decimal(0)) / len(refunds))
        return [Recommendation(
            id="reduce-overwithholding",
            title=f"You've averaged ~${int(avg):,} in refunds — adjust your W-4",
            severity="info",
            category="compliance",
            rationale=(
                f"Refunds across the last {len(refunds)} years averaged ${int(avg):,}. "
                "A refund is just a 0%-interest loan you made to the Treasury. "
                "Investing that money during the year (or paying down debt) is strictly better."
            ),
            action="File a new W-4 with HR to increase exemptions / dependents claimed.",
            est_annual_savings=_dollars(avg * Decimal("0.045")),  # ~T-bill yield
            references=["Form W-4"],
        )]
    return []


def rule_capital_gain_trend(history: list[tuple[Return, TaxResult]]) -> list[Recommendation]:
    """Rising LTCG every year = strong TLH candidate."""
    if len(history) < 3:
        return []
    series = [r.long_term_capital_gains + r.short_term_capital_gains for r, _ in history[-3:]]
    if not (series[0] < series[1] < series[2]) or series[-1] < 25_000:
        return []
    rules = load_rules(history[-1][0].tax_year)
    marg = _marginal_ordinary_rate(history[-1][1].taxable_income, rules, history[-1][0].filing_status.value)
    return [Recommendation(
        id="rising-capital-gains",
        title="Capital gains growing every year — institute a TLH discipline",
        severity="suggested",
        category="investments",
        rationale=(
            f"Realized gains rose from ${int(series[0]):,} → ${int(series[1]):,} → "
            f"${int(series[2]):,}. Establishing a year-end TLH routine (and writing the "
            "wash-sale-replacement pairs ahead of time) can clip ~$3k of ordinary income "
            "every year and bank further losses for future bills."
        ),
        action="Schedule a Nov + Dec TLH review; document replacement tickers in advance.",
        est_annual_savings=_dollars(Decimal(3_000) * marg),
        references=["IRC §1211(b)", "IRC §1091"],
    )]


MULTI_RULES = [rule_roth_conversion_window, rule_persistent_refund, rule_capital_gain_trend]


def advise_multi(history: Iterable[tuple[Return, TaxResult]]) -> list[Recommendation]:
    hist = sorted(history, key=lambda p: p[0].tax_year)
    out: list[Recommendation] = []
    for rule in MULTI_RULES:
        try:
            out.extend(rule(hist))
        except Exception:
            continue
    out.sort(key=lambda r: r.est_annual_savings, reverse=True)
    return out
