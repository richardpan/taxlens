"""Tax Savings Advisor — pattern-based recommendations from a return + computed result.

Each rule is a small pure function that takes (Return, TaxResult, Rules) and either
returns None (rule doesn't apply) or a `Recommendation`. The engine here just runs
them all and ranks by estimated annual savings.

Design notes:
  * No I/O, no side effects — same purity contract as the tax engine.
  * Marginal-rate computations use the OR­DINARY brackets and the filer's taxable
    income as the "starting" point — a reasonable approximation good enough for
    "this is the size of the opportunity" headline numbers.
  * All money is Decimal; all rule outputs are rounded to the nearest dollar in
    the `est_annual_savings` field (cent precision is false confidence here).
  * Multi-year rules (Roth conversion windows, bunching detection across years)
    live in `advisor_multi.py` and consume a list of (Return, TaxResult).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable, Optional

from taxlens.models import FilingStatus, Return, Rules, TaxResult
from taxlens.rules import load_rules


ZERO = Decimal(0)
DOLLAR = Decimal(1)


def _dollars(x: Decimal) -> Decimal:
    return x.quantize(DOLLAR, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Recommendation:
    id: str                       # stable machine-readable id, e.g. "max-401k"
    title: str                    # short headline
    severity: str                 # "info" | "suggested" | "high"
    category: str                 # "retirement" | "deductions" | "investments" | "structure" | "compliance"
    rationale: str                # 1-3 sentences explaining why this applies
    action: str                   # concrete next step
    est_annual_savings: Decimal   # rough $ figure, rounded to whole dollars
    references: list[str]         # IRS pubs / form numbers for the curious

    def to_dict(self) -> dict:
        d = asdict(self)
        d["est_annual_savings"] = str(self.est_annual_savings)
        return d


# ──────────────────── marginal-rate helpers ────────────────────

def _marginal_ordinary_rate(taxable: Decimal, rules: Rules, status: str) -> Decimal:
    """Marginal federal ordinary rate at `taxable` income."""
    brackets = rules.ordinary_brackets[status]
    rate = brackets[0][1]
    for low, r in brackets:
        if taxable >= low:
            rate = r
        else:
            break
    return rate


def _next_bracket_room(taxable: Decimal, rules: Rules, status: str) -> Decimal:
    """How many more dollars before crossing into the next higher ordinary bracket."""
    brackets = rules.ordinary_brackets[status]
    for low, _ in brackets:
        if low > taxable:
            return low - taxable
    return Decimal("Infinity")


# ──────────────────── individual rules ────────────────────

def rule_max_401k(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    limits = (rules.contribution_limits or {})
    cap = Decimal(limits.get("k401_elective_deferral", 23_000))
    deferred = ret.traditional_401k_contributions + ret.roth_401k_contributions
    if ret.wages < 10_000:
        return None  # no wages → no 401k to contribute through
    gap = max(ZERO, cap - deferred)
    if gap < 1_000:
        return None
    marg = _marginal_ordinary_rate(result.taxable_income, rules, ret.filing_status.value)
    # Only TRADITIONAL contributions reduce current-year tax; recommend the deductible share.
    savings = _dollars(gap * marg)
    return Recommendation(
        id="max-401k",
        title=f"Contribute another ${int(gap):,} to your traditional 401(k)",
        severity="high" if savings >= 1_500 else "suggested",
        category="retirement",
        rationale=(
            f"You contributed ${int(deferred):,} to a 401(k) this year vs the "
            f"${int(cap):,} IRS limit. At your {marg*100:.0f}% marginal rate, each "
            f"additional dollar saves about {marg*100:.0f}¢ in federal tax."
        ),
        action=(
            f"Increase payroll deferral so you hit the ${int(cap):,} cap before "
            "Dec 31. If you're 50+, you can add another $7,500 catch-up."
        ),
        est_annual_savings=savings,
        references=["IRC §402(g)", "IRS Pub. 560"],
    )


def rule_max_hsa(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    limits = (rules.contribution_limits or {})
    # We don't know coverage type — assume family if MFJ, else self.
    if ret.filing_status == FilingStatus.MFJ:
        cap = Decimal(limits.get("hsa_family", 8_300))
        ctype = "family"
    else:
        cap = Decimal(limits.get("hsa_self", 4_150))
        ctype = "self-only"
    contrib = ret.hsa_deduction + ret.hsa_contributions
    gap = max(ZERO, cap - contrib)
    if gap < 500:
        return None
    marg = _marginal_ordinary_rate(result.taxable_income, rules, ret.filing_status.value)
    savings = _dollars(gap * marg)
    return Recommendation(
        id="max-hsa",
        title=f"Top up your HSA by ${int(gap):,}",
        severity="suggested",
        category="retirement",
        rationale=(
            f"Assuming {ctype} HDHP coverage, you're ${int(gap):,} below the IRS "
            f"HSA cap of ${int(cap):,}. HSA dollars are triple-tax-advantaged "
            "(deduct now, grow tax-free, withdraw tax-free for medical)."
        ),
        action="Contribute the remainder before April 15 next year (HSAs have a long contribution window).",
        est_annual_savings=savings,
        references=["IRS Pub. 969", "Form 8889"],
    )


def rule_backdoor_roth(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    limits = (rules.contribution_limits or {})
    pe = (limits.get("roth_ira_phaseout_end") or {})
    if not pe:
        return None
    end = Decimal(pe.get(ret.filing_status.value, 0))
    if end <= 0 or result.agi < end:
        return None
    if ret.roth_ira_contributions > 0:
        return Recommendation(
            id="backdoor-roth-warning",
            title="Direct Roth IRA contributions appear ineligible at your income",
            severity="high",
            category="compliance",
            rationale=(
                f"Your AGI of ${int(result.agi):,} is above the Roth-IRA phaseout "
                f"endpoint of ${int(end):,} for {ret.filing_status.value.upper()}. "
                "Direct Roth contributions would be excess and incur a 6% penalty."
            ),
            action="Re-characterize the contribution as a non-deductible traditional IRA, then convert it (the 'backdoor Roth' two-step).",
            est_annual_savings=_dollars(ret.roth_ira_contributions * Decimal("0.06")),
            references=["Form 8606", "IRC §408A(c)(3)"],
        )
    return Recommendation(
        id="backdoor-roth",
        title="Backdoor Roth opportunity — your income exceeds the direct-Roth limit",
        severity="suggested",
        category="retirement",
        rationale=(
            f"Your AGI (${int(result.agi):,}) is above the Roth-IRA direct-contribution "
            f"phaseout of ${int(end):,}. You can still get money into a Roth via the "
            "two-step backdoor: contribute non-deductibly to a traditional IRA, then convert."
        ),
        action="Contribute the annual IRA cap (currently $7,000) non-deductibly, then convert to Roth same day. Watch for the IRA aggregation rule if you have pre-tax IRA balances.",
        est_annual_savings=ZERO,  # no current-year savings; long-term growth benefit
        references=["Form 8606", "IRS Notice 2014-54"],
    )


def rule_bunching_donations(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    """If filer takes the std deduction but has notable itemizable items, suggest bunching."""
    if result.deduction_kind != "standard":
        return None
    itemizable = (
        ret.charitable_contributions + ret.mortgage_interest
        + min(ret.salt_paid, Decimal(10_000))
    )
    std = result.deduction_used
    if itemizable < std * Decimal("0.6"):
        return None
    # Estimate: bunching 2 years of charity into one + std the next ≈ saves marginal × (excess over std)
    bunched = (itemizable + ret.charitable_contributions)  # double up charity
    excess = max(ZERO, bunched - std)
    if excess < 1_000:
        return None
    marg = _marginal_ordinary_rate(result.taxable_income, rules, ret.filing_status.value)
    savings = _dollars(excess * marg / 2)  # amortize across two years
    return Recommendation(
        id="bunching-donations",
        title="Consider bunching charitable donations into alternating years",
        severity="suggested",
        category="deductions",
        rationale=(
            f"You took the ${int(std):,} standard deduction, but your itemizable items "
            f"(charity ${int(ret.charitable_contributions):,}, mortgage interest "
            f"${int(ret.mortgage_interest):,}, SALT ${int(min(ret.salt_paid, Decimal(10_000))):,}) "
            "total close to the standard. Doubling up donations every other year (or via "
            "a donor-advised fund) lets you itemize that year and take the standard the next."
        ),
        action="Open a donor-advised fund and front-load 2 years of charity into it this calendar year.",
        est_annual_savings=savings,
        references=["IRC §170", "IRS Pub. 526"],
    )


def rule_tlh_opportunity(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    gains = ret.long_term_capital_gains + ret.short_term_capital_gains \
        + ret.k1_long_term_gains + ret.k1_short_term_gains
    if gains < 10_000:
        return None
    marg = _marginal_ordinary_rate(result.taxable_income, rules, ret.filing_status.value)
    # Up to $3,000 of net losses offset ordinary income; further losses offset gains 1:1.
    savings_floor = _dollars(min(gains, Decimal(3_000)) * marg)
    return Recommendation(
        id="tax-loss-harvest",
        title="Look for tax-loss-harvesting candidates in your taxable accounts",
        severity="suggested",
        category="investments",
        rationale=(
            f"You realized about ${int(gains):,} of capital gains. Selling positions "
            "currently at a loss and replacing them with similar-but-not-identical "
            "holdings nets your gains down — up to $3,000 of excess loss can offset "
            "ordinary income, and the rest carries forward."
        ),
        action="Run a TLH scan in your brokerage. Beware the 30-day wash-sale rule across all your accounts (incl. spouse + IRA).",
        est_annual_savings=savings_floor,
        references=["IRC §1211(b)", "IRC §1091 (wash sales)"],
    )


def rule_amt_planning(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    if result.amt < 500:
        return None
    return Recommendation(
        id="amt-iso-staggering",
        title=f"AMT of ${int(result.amt):,} this year — consider staggering ISO exercises",
        severity="high",
        category="structure",
        rationale=(
            f"You owe ${int(result.amt):,} of Alternative Minimum Tax, often driven "
            f"by ${int(ret.iso_bargain_element):,} of ISO bargain element and "
            f"${int(ret.amt_preferences):,} of other preferences. Exercising ISOs "
            "across multiple tax years can keep each year under the AMT crossover point."
        ),
        action="Model partial ISO exercises year-by-year using TaxLens's what-if; aim for AMT = $0 per year.",
        est_annual_savings=result.amt,  # the AMT itself is the opportunity envelope
        references=["Form 6251", "IRC §55", "IRC §422"],
    )


def rule_s_corp_election(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    """Sole proprietors with high SE income can save SE tax via S-corp salary split."""
    if ret.se_income < 80_000:
        return None
    if result.se_tax < 10_000:
        return None
    # Conservative model: 60% salary / 40% distribution → distribution portion saves 15.3%
    distribution_share = (ret.se_income * Decimal("0.40"))
    saved = _dollars(distribution_share * Decimal("0.153"))
    return Recommendation(
        id="s-corp-election",
        title="Consider an S-corp election to reduce self-employment tax",
        severity="suggested",
        category="structure",
        rationale=(
            f"You paid ${int(result.se_tax):,} in SE tax on ${int(ret.se_income):,} "
            "of Schedule C income. An S-corp election lets you pay yourself a "
            "reasonable W-2 salary (subject to FICA) and take the remainder as "
            "distributions (no SE tax). Setup + payroll adds ~$2k/yr of overhead."
        ),
        action="Talk to a CPA about Form 2553 and your industry's 'reasonable compensation' benchmark.",
        est_annual_savings=saved,
        references=["Form 2553", "IRC §1366"],
    )


def rule_estimated_tax_safe_harbor(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    paid = ret.federal_withholding + ret.estimated_payments
    owed = max(ZERO, result.total_tax - paid)
    if owed < 1_000:
        return None
    return Recommendation(
        id="estimated-tax-safe-harbor",
        title=f"You owe ${int(owed):,} at filing — watch the underpayment penalty",
        severity="info" if owed < 5_000 else "high",
        category="compliance",
        rationale=(
            f"Total tax ${int(result.total_tax):,}, withholding+estimated ${int(paid):,}. "
            "The IRS underpayment penalty applies unless you've paid in either 90% of "
            "this year's tax OR 100% of last year's (110% if AGI > $150k) by quarterly deadlines."
        ),
        action="Either bump payroll withholding for next year or set up quarterly 1040-ES payments hitting safe-harbor.",
        est_annual_savings=ZERO,
        references=["Form 2210", "IRC §6654"],
    )


def rule_qbi_under_threshold(ret: Return, result: TaxResult, rules: Rules) -> Optional[Recommendation]:
    """If filer has QBI but threshold blew them out, suggest income-smoothing."""
    qbi_eligible = ret.k1_section_199a_qbi + max(ZERO, ret.se_income) + max(ZERO, ret.rental_net_income)
    if qbi_eligible <= 0:
        return None
    if result.qbi_deduction >= qbi_eligible * Decimal("0.19"):
        return None  # already getting ~full 20%
    cfg = rules.qbi or {}
    thr = Decimal((cfg.get("threshold") or {}).get(ret.filing_status.value, 0))
    if thr <= 0 or result.taxable_income <= thr:
        return None
    marg = _marginal_ordinary_rate(result.taxable_income, rules, ret.filing_status.value)
    lost = (qbi_eligible * Decimal("0.20")) - result.qbi_deduction
    savings = _dollars(lost * marg)
    return Recommendation(
        id="qbi-income-smoothing",
        title="Your QBI deduction is reduced by the income phaseout",
        severity="suggested",
        category="structure",
        rationale=(
            "Your taxable income exceeds the §199A threshold, which limits or eliminates "
            "the 20% QBI deduction (especially for SSTBs). Defer income or accelerate "
            "deductions to drop under the threshold."
        ),
        action="Increase 401(k)/HSA contributions, accelerate equipment purchases (§179), or defer year-end invoicing.",
        est_annual_savings=savings,
        references=["Form 8995-A", "IRC §199A"],
    )


# ──────────────────── public entry ────────────────────

ALL_RULES: list[Callable[[Return, TaxResult, Rules], Optional[Recommendation]]] = [
    rule_max_401k,
    rule_max_hsa,
    rule_backdoor_roth,
    rule_bunching_donations,
    rule_tlh_opportunity,
    rule_amt_planning,
    rule_s_corp_election,
    rule_estimated_tax_safe_harbor,
    rule_qbi_under_threshold,
]


def advise(ret: Return, result: TaxResult, rules: Rules | None = None) -> list[Recommendation]:
    """Run every rule and return matches sorted by est_annual_savings desc."""
    rules = rules or load_rules(ret.tax_year)
    recs: list[Recommendation] = []
    for rule in ALL_RULES:
        try:
            r = rule(ret, result, rules)
        except Exception:
            # A buggy individual rule must never break the whole advisor.
            continue
        if r is not None:
            recs.append(r)
    recs.sort(key=lambda r: r.est_annual_savings, reverse=True)
    return recs
