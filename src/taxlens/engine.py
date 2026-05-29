"""Core tax engine. Pure functions, Decimal money, full audit trail.

Architectural rules:
  * No I/O. Caller passes a `Return` and (optionally) loaded `Rules`.
  * Every dollar of `total_tax` must be reproducible from `steps` + bracket fills.
  * Never silently "correct" the user's return; if `reported_total_tax` is set,
    record the delta in `reconciliation_delta` and let the UI surface it.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from taxlens.brackets import walk_brackets
from taxlens.models import (
    BracketFill,
    ComputationStep,
    Return,
    Rules,
    TaxResult,
)
from taxlens.rules import load_rules

ZERO = Decimal(0)
CENT = Decimal("0.01")


def _money(x: Decimal) -> Decimal:
    """Round to the nearest cent using bankers'... no, IRS uses HALF_UP."""
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _status(ret: Return) -> str:
    return ret.filing_status.value


class _StepRecorder:
    """Accumulates ComputationStep entries in declaration order."""

    def __init__(self) -> None:
        self._steps: list[ComputationStep] = []

    def add(self, label: str, formula: str, inputs: dict[str, Any], output: Decimal) -> Decimal:
        rounded = _money(output)
        self._steps.append(
            ComputationStep(
                index=len(self._steps) + 1,
                label=label,
                formula=formula,
                inputs={k: str(v) if isinstance(v, Decimal) else v for k, v in inputs.items()},
                output=rounded,
            )
        )
        return rounded

    @property
    def steps(self) -> list[ComputationStep]:
        return list(self._steps)


# ────────────────────────── individual computation stages ──────────────────────────

def _compute_se_tax(ret: Return, rules: Rules, rec: _StepRecorder) -> tuple[Decimal, Decimal]:
    """Returns (se_tax, deductible_half) — half of SE tax is an above-the-line adjustment."""
    if ret.se_income <= 0:
        return ZERO, ZERO

    se = rules.se_tax
    base = ret.se_income * se["net_earnings_multiplier"]
    ss_taxable = min(base, se["social_security_wage_base"])
    ss_tax = ss_taxable * se["social_security_rate"]
    medicare_tax = base * se["medicare_rate"]
    se_tax = ss_tax + medicare_tax

    rec.add(
        "SE tax base",
        "se_income × 0.9235",
        {"se_income": ret.se_income, "multiplier": se["net_earnings_multiplier"]},
        base,
    )
    rec.add(
        "SE Social Security portion",
        f"min(base, {se['social_security_wage_base']}) × {se['social_security_rate']}",
        {"base": base, "wage_base": se["social_security_wage_base"], "rate": se["social_security_rate"]},
        ss_tax,
    )
    rec.add(
        "SE Medicare portion",
        f"base × {se['medicare_rate']}",
        {"base": base, "rate": se["medicare_rate"]},
        medicare_tax,
    )
    se_tax_rounded = rec.add("SE tax total", "ss + medicare", {"ss": ss_tax, "medicare": medicare_tax}, se_tax)

    deductible = se_tax * se["deductible_fraction"]
    rec.add(
        "½ SE tax (above-the-line deduction)",
        f"se_tax × {se['deductible_fraction']}",
        {"se_tax": se_tax, "fraction": se["deductible_fraction"]},
        deductible,
    )
    return se_tax_rounded, _money(deductible)


def _compute_agi(ret: Return, half_se_tax: Decimal, rec: _StepRecorder) -> Decimal:
    gross = (
        ret.wages
        + ret.interest_income
        + ret.ordinary_dividends
        + ret.long_term_capital_gains
        + ret.short_term_capital_gains
        + ret.se_income
        + ret.other_ordinary_income
    )
    rec.add(
        "Gross income",
        "wages + interest + ord_div + ltcg + stcg + se + other",
        {
            "wages": ret.wages,
            "interest": ret.interest_income,
            "ord_div": ret.ordinary_dividends,
            "ltcg": ret.long_term_capital_gains,
            "stcg": ret.short_term_capital_gains,
            "se": ret.se_income,
            "other": ret.other_ordinary_income,
        },
        gross,
    )
    adjustments = ret.hsa_deduction + ret.other_adjustments + half_se_tax
    rec.add(
        "Above-the-line adjustments",
        "hsa + other + ½ se_tax",
        {"hsa": ret.hsa_deduction, "other": ret.other_adjustments, "half_se_tax": half_se_tax},
        adjustments,
    )
    agi = gross - adjustments
    return rec.add("AGI", "gross − adjustments", {"gross": gross, "adjustments": adjustments}, agi)


def _compute_taxable_income(
    ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder
) -> tuple[Decimal, Decimal, str]:
    std = rules.standard_deduction[_status(ret)]
    if ret.itemized_deductions is not None and ret.itemized_deductions > std:
        deduction = ret.itemized_deductions
        kind = "itemized"
    else:
        deduction = std
        kind = "standard"
    rec.add(
        f"{kind.capitalize()} deduction",
        f"{kind} ({_status(ret).upper()}, {ret.tax_year})",
        {"kind": kind, "amount": deduction, "standard": std, "itemized": ret.itemized_deductions},
        deduction,
    )
    taxable = max(ZERO, agi - deduction)
    rec.add(
        "Taxable income",
        "max(0, agi − deduction)",
        {"agi": agi, "deduction": deduction},
        taxable,
    )
    return taxable, deduction, kind


def _compute_income_tax(
    ret: Return, taxable: Decimal, rules: Rules, rec: _StepRecorder
) -> tuple[Decimal, Decimal, list[BracketFill], list[BracketFill]]:
    """Apply ordinary brackets to (taxable − qualified), then stack qualified income on top."""
    status = _status(ret)
    qualified_income = min(
        taxable, ret.qualified_dividends + ret.long_term_capital_gains
    )
    ordinary_taxable = taxable - qualified_income

    ord_tax, ord_fills = walk_brackets(ordinary_taxable, rules.ordinary_brackets[status])
    rec.add(
        "Ordinary income tax (bracket walk)",
        "sum of bracket fills on (taxable − qualified)",
        {"ordinary_taxable": ordinary_taxable, "brackets": len(ord_fills)},
        ord_tax,
    )

    qual_tax, qual_fills = walk_brackets(
        qualified_income,
        rules.qualified_brackets[status],
        stack_above=ordinary_taxable,
    )
    rec.add(
        "Qualified income tax (stacked above ordinary)",
        "qualified bracket walk stacked above ordinary_taxable",
        {
            "qualified_income": qualified_income,
            "stack_above": ordinary_taxable,
            "brackets": len(qual_fills),
        },
        qual_tax,
    )
    return _money(ord_tax), _money(qual_tax), ord_fills, qual_fills


def _compute_additional_medicare(ret: Return, rules: Rules, rec: _StepRecorder) -> Decimal:
    cfg = rules.additional_medicare
    threshold = Decimal(cfg["threshold"][_status(ret)])
    rate = Decimal(cfg["rate"])
    medicare_wages = ret.wages + ret.se_income * rules.se_tax["net_earnings_multiplier"]
    excess = max(ZERO, medicare_wages - threshold)
    tax = excess * rate
    rec.add(
        "Additional Medicare Tax (Form 8959)",
        f"max(0, wages + 0.9235·se − {threshold}) × {rate}",
        {"medicare_wages": medicare_wages, "threshold": threshold, "rate": rate},
        tax,
    )
    return _money(tax)


def _compute_niit(ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder) -> Decimal:
    cfg = rules.niit
    threshold = Decimal(cfg["threshold"][_status(ret)])
    rate = Decimal(cfg["rate"])
    investment_income = (
        ret.interest_income
        + ret.ordinary_dividends
        + ret.long_term_capital_gains
        + ret.short_term_capital_gains
    )
    magi_excess = max(ZERO, agi - threshold)
    niit_base = min(investment_income, magi_excess)
    tax = niit_base * rate
    rec.add(
        "Net Investment Income Tax (Form 8960)",
        f"min(investment_income, max(0, agi − {threshold})) × {rate}",
        {"investment_income": investment_income, "magi_excess": magi_excess, "rate": rate},
        tax,
    )
    return _money(tax)


def _compute_ctc(ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder) -> Decimal:
    cfg = rules.ctc
    if ret.qualifying_children <= 0:
        return ZERO
    per_child = Decimal(cfg["per_qualifying_child"])
    raw = per_child * ret.qualifying_children
    threshold = Decimal(cfg["phaseout_start"][_status(ret)])
    if agi > threshold:
        # IRS rounds AGI excess UP to the next $1,000.
        over = ((agi - threshold) / 1000).to_integral_value(rounding="ROUND_CEILING")
        reduction = over * Decimal(cfg["phaseout_per_1000_agi"])
        credit = max(ZERO, raw - reduction)
    else:
        reduction = ZERO
        credit = raw
    rec.add(
        "Child Tax Credit",
        "per_child × children − phaseout",
        {
            "per_child": per_child,
            "children": ret.qualifying_children,
            "raw": raw,
            "agi": agi,
            "threshold": threshold,
            "reduction": reduction,
        },
        credit,
    )
    return _money(credit)


# ────────────────────────── public entry point ──────────────────────────

def compute(ret: Return, rules: Rules | None = None) -> TaxResult:
    """Run the full federal tax computation for one return."""
    rules = rules or load_rules(ret.tax_year)
    if rules.year != ret.tax_year:
        raise ValueError(f"rules year {rules.year} ≠ return year {ret.tax_year}")

    rec = _StepRecorder()

    se_tax, half_se_tax = _compute_se_tax(ret, rules, rec)
    agi = _compute_agi(ret, half_se_tax, rec)
    taxable, deduction, deduction_kind = _compute_taxable_income(ret, agi, rules, rec)
    ord_tax, qual_tax, ord_fills, qual_fills = _compute_income_tax(ret, taxable, rules, rec)
    addl_medicare = _compute_additional_medicare(ret, rules, rec)
    niit = _compute_niit(ret, agi, rules, rec)
    credits = _compute_ctc(ret, agi, rules, rec)

    total_tax = ord_tax + qual_tax + se_tax + addl_medicare + niit - credits
    total_tax = max(ZERO, total_tax)
    rec.add(
        "Total tax",
        "ordinary + qualified + se + addl_medicare + niit − credits",
        {
            "ordinary": ord_tax,
            "qualified": qual_tax,
            "se": se_tax,
            "addl_medicare": addl_medicare,
            "niit": niit,
            "credits": credits,
        },
        total_tax,
    )

    payments = ret.federal_withholding + ret.estimated_payments
    refund = payments - total_tax
    rec.add(
        "Refund (+) / owed (−)",
        "withholding + estimated_payments − total_tax",
        {"withholding": ret.federal_withholding, "estimated": ret.estimated_payments, "total_tax": total_tax},
        refund,
    )

    delta = None
    if ret.reported_total_tax is not None:
        delta = _money(total_tax - ret.reported_total_tax)

    return TaxResult(
        tax_year=ret.tax_year,
        filing_status=ret.filing_status,
        agi=_money(agi),
        taxable_income=_money(taxable),
        deduction_used=_money(deduction),
        deduction_kind=deduction_kind,
        ordinary_tax=ord_tax,
        qualified_tax=qual_tax,
        se_tax=se_tax,
        additional_medicare_tax=addl_medicare,
        niit=niit,
        credits=credits,
        total_tax=_money(total_tax),
        refund_or_owed=_money(refund),
        ordinary_bracket_fills=ord_fills,
        qualified_bracket_fills=qual_fills,
        steps=rec.steps,
        reported_total_tax=ret.reported_total_tax,
        reconciliation_delta=delta,
    )
