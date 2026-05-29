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
    StateResult,
    StateRules,
    TaxResult,
)
from taxlens.rules import load_rules, load_state_rules, load_locality_rules

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

def _compute_schedule_e(ret: Return, rec: _StepRecorder) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (net_schedule_e, passive_loss_disallowed, new_pal_carryforward).

    Simplified Form 8582 model:
      - Rentals are passive by default. Up to $25,000 of rental losses are
        allowed against non-passive income for active participants (phased
        out 50¢/$1 over modified AGI $100k, fully gone at $150k — we apply
        the phaseout against gross income proxy = wages, since we haven't
        computed AGI yet at this stage; it's a defensible simplification).
      - Royalties are non-passive.
      - K-1 ordinary business income is treated as non-passive here (most
        active S-corp/LLC owners). Passive K-1 is a v1.x refinement.
      - Suspended losses from prior years are released up to the allowance.
    """
    royalties = ret.royalty_income
    k1_active = ret.k1_ordinary_business_income
    rental = ret.rental_net_income
    suspended_in = ret.suspended_passive_losses_carryforward

    rental_for_offset = rental - suspended_in  # negative = additional loss
    if rental_for_offset >= 0:
        # Net positive: prior suspended losses fully absorbed; nothing carries.
        allowed_loss = ZERO
        new_carry = ZERO
        net_passive = rental_for_offset
    else:
        # We have a passive loss. Apply $25k active-participation allowance with phaseout.
        loss = -rental_for_offset
        if ret.is_active_real_estate_participant:
            magi_proxy = ret.wages + ret.k1_ordinary_business_income
            phaseout_start = Decimal(100_000)
            phaseout_end = Decimal(150_000)
            if magi_proxy <= phaseout_start:
                allowance = Decimal(25_000)
            elif magi_proxy >= phaseout_end:
                allowance = ZERO
            else:
                allowance = Decimal(25_000) - (magi_proxy - phaseout_start) * Decimal("0.5")
        else:
            allowance = ZERO
        allowed_loss = min(loss, allowance)
        new_carry = loss - allowed_loss
        net_passive = -allowed_loss

    net_e = royalties + k1_active + net_passive
    rec.add(
        "Schedule E net (rental + royalty + K-1 active)",
        "royalty + k1_obi + min(rental_net_after_carryover, allowed_passive_loss)",
        {
            "royalty": royalties, "k1_obi": k1_active, "rental_net": rental,
            "suspended_in": suspended_in, "new_carry": new_carry,
        },
        net_e,
    )
    return net_e, _money(new_carry), _money(new_carry)


def _compute_qbi(ret: Return, taxable_before_qbi: Decimal, rules: Rules, rec: _StepRecorder) -> Decimal:
    """Form 8995 simplified — 20% of qualified business income, capped at 20%
    of (taxable income − net capital gain). SSTB phaseouts use the income
    thresholds in rules.qbi.threshold; below threshold, SSTBs qualify too."""
    qbi_eligible = (
        ret.k1_section_199a_qbi
        + (ret.se_income if ret.se_income > 0 else ZERO)
        + (ret.rental_net_income if ret.rental_net_income > 0 else ZERO)
    )
    if qbi_eligible <= 0:
        return ZERO

    cfg = rules.qbi or {}
    rate = Decimal(cfg.get("rate", "0.20"))
    thresholds = cfg.get("threshold", {})
    threshold = Decimal(thresholds.get(_status(ret), 0)) if thresholds else ZERO
    phaseout = Decimal(cfg.get("phaseout", 50_000)) if _status(ret) != "mfj" \
        else Decimal(cfg.get("phaseout_mfj", 100_000))

    # SSTB phaseout
    if ret.k1_is_sstb and threshold > 0:
        if taxable_before_qbi >= threshold + phaseout:
            qbi_eligible = ZERO
        elif taxable_before_qbi > threshold:
            scale = (threshold + phaseout - taxable_before_qbi) / phaseout
            qbi_eligible = qbi_eligible * scale

    net_cap_gain = (
        ret.long_term_capital_gains + ret.qualified_dividends + ret.k1_long_term_gains
        + ret.k1_qualified_dividends
    )
    cap1 = qbi_eligible * rate
    cap2 = max(ZERO, (taxable_before_qbi - net_cap_gain)) * rate
    qbi_ded = min(cap1, cap2)
    rec.add(
        "QBI deduction (Section 199A)",
        "min(0.20 × QBI, 0.20 × (taxable − net cap gain))",
        {"qbi_eligible": qbi_eligible, "taxable": taxable_before_qbi, "net_cap_gain": net_cap_gain},
        qbi_ded,
    )
    return _money(qbi_ded)


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


def _compute_agi(ret: Return, half_se_tax: Decimal, sch_e_net: Decimal, rec: _StepRecorder) -> tuple[Decimal, Decimal]:
    """Returns (AGI, capital_loss_carryforward_out)."""
    # Net capital gain/loss with the §1211(b) $3,000 ordinary-income cap on
    # net losses. Excess carries forward indefinitely (§1212(b)).
    raw_cap = (
        ret.long_term_capital_gains + ret.k1_long_term_gains
        + ret.short_term_capital_gains + ret.k1_short_term_gains
    )
    # Apply prior-year carryforward (treated as additional LT loss for simplicity).
    carryforward_in = ret.capital_loss_carryforward_in or ZERO
    if carryforward_in > 0:
        raw_cap = raw_cap - carryforward_in
        rec.add(
            "Apply prior-year capital-loss carryforward",
            "raw_cap − carryforward_in",
            {"carryforward_in": carryforward_in},
            raw_cap,
        )

    carryforward_out = ZERO
    if raw_cap < Decimal("-3000"):
        net_cap = Decimal("-3000")
        carryforward_out = -(raw_cap - net_cap)  # positive
        rec.add(
            "Capital loss limitation (§1211(b))",
            "net loss capped at -$3,000; excess carries forward (§1212(b))",
            {"raw_cap": raw_cap, "carryforward_out": carryforward_out},
            net_cap,
        )
    else:
        net_cap = raw_cap

    gross = (
        ret.wages
        + ret.interest_income + ret.k1_interest
        + ret.ordinary_dividends + ret.k1_ordinary_dividends
        + net_cap
        + ret.se_income
        + ret.other_ordinary_income
        + sch_e_net
    )
    rec.add(
        "Gross income",
        "wages + interest(+k1) + ord_div(+k1) + net_cap + se + sch_e + other",
        {
            "wages": ret.wages,
            "interest": ret.interest_income, "k1_interest": ret.k1_interest,
            "ord_div": ret.ordinary_dividends, "k1_ord_div": ret.k1_ordinary_dividends,
            "net_cap": net_cap,
            "se": ret.se_income,
            "sch_e": sch_e_net,
            "other": ret.other_ordinary_income,
        },
        gross,
    )
    # Traditional IRA contributions are above-the-line (we ignore the active-participant
    # deductibility phaseout here — the Advisor warns about it explicitly).
    adjustments = (
        ret.hsa_deduction
        + ret.traditional_ira_contributions
        + ret.other_adjustments
        + half_se_tax
    )
    rec.add(
        "Above-the-line adjustments",
        "hsa + trad_ira + other + ½ se_tax",
        {
            "hsa": ret.hsa_deduction,
            "trad_ira": ret.traditional_ira_contributions,
            "other": ret.other_adjustments,
            "half_se_tax": half_se_tax,
        },
        adjustments,
    )
    agi = gross - adjustments
    rec.add("AGI", "gross − adjustments", {"gross": gross, "adjustments": adjustments}, agi)
    return agi, carryforward_out


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
) -> tuple[Decimal, Decimal, Decimal, Decimal, list[BracketFill], list[BracketFill]]:
    """Stacked income-tax computation honoring Schedule D worksheet cap rates.

    Order of stacking (bottom → top of taxable income):
      1. Ordinary income  (regular brackets)
      2. Unrecaptured §1250 gain (capped at 25%)
      3. Collectibles gain (capped at 28%)
      4. LTCG + qualified dividends (0/15/20%)

    Each "capped" bucket is taxed at the lesser of its cap rate and the
    marginal ordinary rate that would otherwise apply — implemented by
    walking the ordinary brackets and clipping each bracket's rate at the cap.
    """
    status = _status(ret)
    ordinary_brackets = rules.ordinary_brackets[status]
    qualified_brackets = rules.qualified_brackets[status]

    qd_ltcg = ret.qualified_dividends + ret.long_term_capital_gains \
        + ret.k1_qualified_dividends + ret.k1_long_term_gains
    qd_ltcg = max(qd_ltcg, ZERO)  # net LT loss flows through AGI (capped at -3k); never taxed at preferential rate
    unrec_1250 = ret.unrecaptured_1250_gains
    collectibles = ret.collectibles_gains

    # Total special-rate income can't exceed taxable income.
    all_special = qd_ltcg + unrec_1250 + collectibles
    all_special = min(all_special, taxable)
    ordinary_taxable = taxable - all_special

    # 1. Ordinary
    ord_tax, ord_fills = walk_brackets(ordinary_taxable, ordinary_brackets)
    rec.add(
        "Ordinary income tax (bracket walk)",
        "sum of bracket fills on (taxable − qd_ltcg − unrec_1250 − collectibles)",
        {"ordinary_taxable": ordinary_taxable, "brackets": len(ord_fills)},
        ord_tax,
    )

    # 2. Unrecaptured §1250: capped 25%
    unrec_tax = ZERO
    cursor = ordinary_taxable
    if unrec_1250 > 0:
        unrec_tax = _capped_rate_tax(
            unrec_1250, ordinary_brackets, stack_above=cursor, cap=rules.unrecaptured_1250_rate
        )
        rec.add(
            "Unrecaptured §1250 gain tax",
            f"min(marginal, {rules.unrecaptured_1250_rate}) × ${unrec_1250} stacked at ${cursor}",
            {"gain": unrec_1250, "cap": rules.unrecaptured_1250_rate, "stack_above": cursor},
            unrec_tax,
        )
        cursor += unrec_1250

    # 3. Collectibles: capped 28%
    coll_tax = ZERO
    if collectibles > 0:
        coll_tax = _capped_rate_tax(
            collectibles, ordinary_brackets, stack_above=cursor, cap=rules.collectibles_rate
        )
        rec.add(
            "Collectibles (28%-rate) gain tax",
            f"min(marginal, {rules.collectibles_rate}) × ${collectibles} stacked at ${cursor}",
            {"gain": collectibles, "cap": rules.collectibles_rate, "stack_above": cursor},
            coll_tax,
        )
        cursor += collectibles

    # 4. Qualified dividends + LTCG (regular 0/15/20)
    qual_tax, qual_fills = walk_brackets(qd_ltcg, qualified_brackets, stack_above=cursor)
    rec.add(
        "Qualified dividends + LTCG (stacked above all other income)",
        "qualified bracket walk stacked above ordinary + 1250 + collectibles",
        {"qd_ltcg": qd_ltcg, "stack_above": cursor, "brackets": len(qual_fills)},
        qual_tax,
    )
    return (
        _money(ord_tax),
        _money(qual_tax),
        _money(coll_tax),
        _money(unrec_tax),
        ord_fills,
        qual_fills,
    )


def _capped_rate_tax(
    amount: Decimal,
    brackets,
    *,
    stack_above: Decimal,
    cap: Decimal,
) -> Decimal:
    """Walk ordinary brackets for `amount` stacked above `stack_above`, but clip
    each bracket's rate at `cap`. Matches the Sch D worksheet treatment."""
    _, fills = walk_brackets(amount, brackets, stack_above=stack_above)
    return sum(
        (f.amount_in_bracket * min(f.rate, cap) for f in fills),
        start=ZERO,
    )


def _compute_amt(
    ret: Return,
    taxable_income: Decimal,
    regular_pre_credit_tax: Decimal,
    rules: Rules,
    rec: _StepRecorder,
) -> Decimal:
    """Form 6251 — simplified.

    Returns the **additional** AMT owed, i.e. max(0, tentative AMT − regular tax).
    AMTI ≈ taxable_income + amt_preferences + amt_adjustments.
    Capital-gains preferential treatment is preserved inside AMT: the engine
    computes 26%/28% on the ordinary portion of AMTI and adds the same LTCG/qual
    tax we already computed. The result matches Form 6251 Part III for filers
    without exotic adjustments.
    """
    if rules.amt is None:
        return ZERO
    status = _status(ret)
    amt = rules.amt

    qd_ltcg = ret.qualified_dividends + ret.long_term_capital_gains \
        + ret.k1_qualified_dividends + ret.k1_long_term_gains
    qd_ltcg = max(qd_ltcg, ZERO)
    # ISO bargain element is one of the most common AMT preference items.
    amti = (
        taxable_income
        + ret.amt_preferences
        + ret.amt_adjustments
        + ret.iso_bargain_element
    )
    amti = max(ZERO, amti)

    exemption = Decimal(amt["exemption"][status])
    phaseout_start = Decimal(amt["exemption_phaseout_start"][status])
    phaseout_rate = Decimal(amt["phaseout_rate"])
    if amti > phaseout_start:
        reduction = (amti - phaseout_start) * phaseout_rate
        exemption = max(ZERO, exemption - reduction)
    rec.add(
        "AMT exemption (post-phaseout)",
        "max(0, base − phaseout_rate × (AMTI − phaseout_start))",
        {"amti": amti, "phaseout_start": phaseout_start, "phaseout_rate": phaseout_rate,
         "base_exemption": Decimal(amt["exemption"][status])},
        exemption,
    )

    amti_after_exemption = max(ZERO, amti - exemption)
    amt_ord_portion = max(ZERO, amti_after_exemption - qd_ltcg)
    rate_break = Decimal(amt["rate_break"][status])
    rate_low = Decimal(amt["rate_low"])
    rate_high = Decimal(amt["rate_high"])

    if amt_ord_portion <= rate_break:
        tmt_ord = amt_ord_portion * rate_low
    else:
        tmt_ord = rate_break * rate_low + (amt_ord_portion - rate_break) * rate_high
    rec.add(
        "AMT on ordinary AMTI (26%/28%)",
        f"first ${rate_break} × {rate_low}, excess × {rate_high}",
        {"amt_ord_portion": amt_ord_portion, "rate_break": rate_break,
         "rate_low": rate_low, "rate_high": rate_high},
        tmt_ord,
    )

    # Preserve LTCG/qual treatment inside AMT.
    qual_in_amt = min(qd_ltcg, amti_after_exemption)
    status_q = status
    qual_brackets = rules.qualified_brackets[status_q]
    qual_amt, _ = walk_brackets(qual_in_amt, qual_brackets, stack_above=amt_ord_portion)
    tentative = tmt_ord + qual_amt
    rec.add(
        "Tentative minimum tax",
        "AMT_ordinary + AMT_qualified_at_capital_rates",
        {"amt_ordinary": tmt_ord, "amt_qualified": qual_amt},
        tentative,
    )

    amt_owed = max(ZERO, tentative - regular_pre_credit_tax)
    rec.add(
        "AMT additional (max 0, tentative − regular)",
        "max(0, tentative − regular_tax_before_credits)",
        {"tentative": tentative, "regular": regular_pre_credit_tax},
        amt_owed,
    )
    return _money(amt_owed)


def _compute_state(ret: Return, agi: Decimal, rules: Rules) -> StateResult | None:
    """Optional state computation. Currently shipped: CA."""
    if not ret.state:
        return None
    srules = load_state_rules(ret.state, ret.tax_year)
    return _compute_state_with(ret, agi, srules)


def _compute_state_with(ret: Return, agi: Decimal, srules: StateRules) -> StateResult:
    """Pure state computation given pre-loaded rules. Most states follow the
    federal AGI starting point and add their own std deduction + brackets.
    CA in particular taxes long-term capital gains as ordinary income."""
    rec = _StepRecorder()
    status = ret.filing_status.value

    state_agi = rec.add("State AGI starting point", "= federal AGI", {"federal_agi": agi}, agi)
    deduction = srules.standard_deduction.get(status, srules.standard_deduction.get("single", ZERO))
    rec.add(f"{srules.state} standard deduction",
            f"({status}, {srules.year})", {"amount": deduction}, deduction)
    taxable = max(ZERO, state_agi - deduction)
    rec.add("State taxable income", "max(0, agi − deduction)",
            {"agi": state_agi, "deduction": deduction}, taxable)

    brackets = srules.ordinary_brackets[status]
    # If the state has its own qualified-income brackets (rare; not CA), apply them.
    qual = ret.qualified_dividends + ret.long_term_capital_gains
    if srules.qualified_brackets and qual > 0:
        qual_brackets = srules.qualified_brackets[status]
        ord_taxable = max(ZERO, taxable - qual)
        ord_tax, ord_fills = walk_brackets(ord_taxable, brackets)
        qual_tax, qual_fills = walk_brackets(qual, qual_brackets, stack_above=ord_taxable)
        rec.add("State ordinary tax", "bracket walk", {"ord_taxable": ord_taxable}, ord_tax)
        rec.add("State qualified tax", "bracket walk", {"qual_income": qual}, qual_tax)
        tax = ord_tax + qual_tax
        fills = ord_fills + qual_fills
    else:
        # CA-style: gains taxed as ordinary income.
        tax, fills = walk_brackets(taxable, brackets)
        rec.add("State tax (gains taxed as ordinary)",
                "bracket walk on full taxable income",
                {"taxable": taxable, "brackets": len(fills)}, tax)

    # Optional surcharges (e.g. CA Mental Health Services Tax — 1% over $1M).
    if srules.mental_health_services_tax:
        s = srules.mental_health_services_tax
        thr = Decimal(s["threshold"])
        rate = Decimal(s["rate"])
        if taxable > thr:
            surcharge = (taxable - thr) * rate
            tax = tax + surcharge
            rec.add(
                f"{srules.state} Mental Health Services Tax",
                f"({taxable} − {thr}) × {rate}",
                {"taxable": taxable, "threshold": thr, "rate": rate},
                surcharge,
            )

    # Optional state-level long-term capital-gains excise tax
    # (e.g. WA 7% on LT gains over the per-status threshold, RCW 82.87).
    if srules.capital_gains_excise_tax:
        c = srules.capital_gains_excise_tax
        thresholds = c.get("threshold_by_status", {})
        thr = Decimal(thresholds.get(status, thresholds.get("single", 0)))
        rate = Decimal(c["rate"])
        lt_gains = ret.long_term_capital_gains or ZERO
        if lt_gains > thr:
            cg_tax = (lt_gains - thr) * rate
            tax = tax + cg_tax
            rec.add(
                f"{srules.state} capital-gains excise tax",
                f"({lt_gains} − {thr}) × {rate}",
                {"lt_gains": lt_gains, "threshold": thr, "rate": rate},
                cg_tax,
            )

    # Optional locality (NYC, Yonkers) layered on top of state tax.
    locality_tax = ZERO
    locality_name: str | None = None
    if ret.locality:
        loc = load_locality_rules(ret.locality, ret.tax_year)
        locality_name = str(loc.get("locality", ret.locality)).upper()
        if loc.get("surcharge_of_state_tax"):
            rate = Decimal(loc["surcharge_of_state_tax"])
            locality_tax = tax * rate
            rec.add(
                f"{locality_name} surcharge",
                f"state_tax × {rate}",
                {"state_tax": tax, "rate": rate},
                locality_tax,
            )
        elif loc.get("ordinary_brackets"):
            loc_brackets = loc["ordinary_brackets"][status]
            locality_tax, loc_fills = walk_brackets(taxable, loc_brackets)
            rec.add(
                f"{locality_name} income tax",
                "bracket walk on state taxable income",
                {"taxable": taxable, "brackets": len(loc_fills)},
                locality_tax,
            )
        tax = tax + locality_tax

    return StateResult(
        state=srules.state,
        state_agi=_money(state_agi),
        state_taxable_income=_money(taxable),
        state_tax=_money(tax),
        state_bracket_fills=fills,
        steps=rec.steps,
        locality=locality_name,
        locality_tax=_money(locality_tax),
    )


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
        ret.interest_income + ret.k1_interest
        + ret.ordinary_dividends + ret.k1_ordinary_dividends
        + ret.long_term_capital_gains + ret.k1_long_term_gains
        + ret.short_term_capital_gains + ret.k1_short_term_gains
        + max(ZERO, ret.rental_net_income)        # passive rental net positive only
        + ret.royalty_income
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
    sch_e_net, pal_carry, _ = _compute_schedule_e(ret, rec)
    agi, capital_loss_carry_out = _compute_agi(ret, half_se_tax, sch_e_net, rec)
    taxable_pre_qbi, deduction, deduction_kind = _compute_taxable_income(ret, agi, rules, rec)
    qbi_ded = _compute_qbi(ret, taxable_pre_qbi, rules, rec)
    taxable = max(ZERO, taxable_pre_qbi - qbi_ded)
    if qbi_ded > 0:
        rec.add("Taxable income after QBI deduction",
                "max(0, taxable_pre_qbi − qbi_ded)",
                {"taxable_pre_qbi": taxable_pre_qbi, "qbi_ded": qbi_ded}, taxable)
    ord_tax, qual_tax, coll_tax, unrec_tax, ord_fills, qual_fills = _compute_income_tax(
        ret, taxable, rules, rec
    )
    pre_amt_regular = ord_tax + qual_tax + coll_tax + unrec_tax
    amt = _compute_amt(ret, taxable, pre_amt_regular, rules, rec)
    addl_medicare = _compute_additional_medicare(ret, rules, rec)
    niit = _compute_niit(ret, agi, rules, rec)
    credits = _compute_ctc(ret, agi, rules, rec)

    total_tax = (pre_amt_regular + amt + se_tax + addl_medicare + niit) - credits
    total_tax = max(ZERO, total_tax)
    rec.add(
        "Total tax",
        "ordinary + qualified + coll + 1250 + amt + se + addl_medicare + niit − credits",
        {
            "ordinary": ord_tax, "qualified": qual_tax,
            "collectibles": coll_tax, "unrecaptured_1250": unrec_tax,
            "amt": amt, "se": se_tax, "addl_medicare": addl_medicare,
            "niit": niit, "credits": credits,
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

    state_result = _compute_state(ret, agi, rules)
    if state_result is not None:
        rec.add(
            f"{state_result.state} state tax (separate computation)",
            "see state_result for full audit trail",
            {"state": state_result.state, "state_tax": state_result.state_tax},
            state_result.state_tax,
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
        collectibles_tax=coll_tax,
        unrecaptured_1250_tax=unrec_tax,
        se_tax=se_tax,
        additional_medicare_tax=addl_medicare,
        niit=niit,
        amt=amt,
        credits=credits,
        total_tax=_money(total_tax),
        refund_or_owed=_money(refund),
        ordinary_bracket_fills=ord_fills,
        qualified_bracket_fills=qual_fills,
        steps=rec.steps,
        state_result=state_result,
        qbi_deduction=qbi_ded,
        schedule_e_income=_money(sch_e_net),
        passive_loss_disallowed=pal_carry,
        capital_loss_carryforward_out=_money(capital_loss_carry_out),
        reported_total_tax=ret.reported_total_tax,
        reconciliation_delta=delta,
    )
