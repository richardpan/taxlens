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
    FilingStatus,
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


def _compute_taxable_ss(
    ret: Return, gross_excl_ss: Decimal, rules: Rules, rec: _StepRecorder
) -> Decimal:
    """§86 Social Security benefits taxability.

    Provisional income = gross_income (excl. SS) + tax_exempt_interest + ½ × SS.
    Below the base threshold: 0% taxable.
    Between base and second: lesser of (½ × SS) or (½ × excess over base).
    Above second: 85% × SS, capped at base-tier + 85% × (PI − second).
    """
    benefits = ret.social_security_benefits
    if benefits <= 0:
        return ZERO

    cfg = rules.social_security or {}
    status = _status(ret)
    base = Decimal(str((cfg.get("base_threshold") or {}).get(status, 0)))
    second = Decimal(str((cfg.get("second_threshold") or {}).get(status, 0)))
    first_rate = Decimal(str(cfg.get("first_tier_rate", "0.50")))
    second_rate = Decimal(str(cfg.get("second_tier_rate", "0.85")))

    if base == 0 and second == 0:
        # No rules configured (e.g. pre-1984 or YAML missing) → leave untaxed.
        return ZERO

    provisional = gross_excl_ss + ret.tax_exempt_interest + (benefits * Decimal("0.5"))

    if provisional <= base:
        taxable = ZERO
    elif provisional <= second:
        taxable = min(benefits * first_rate, (provisional - base) * first_rate)
    else:
        tier1_max = (second - base) * first_rate
        taxable = min(
            benefits * second_rate,
            tier1_max + (provisional - second) * second_rate,
        )

    rec.add(
        "Social Security benefits — taxable portion (§86)",
        "tiered 0%/50%/85% on provisional income",
        {
            "benefits": benefits,
            "tax_exempt_interest": ret.tax_exempt_interest,
            "provisional_income": provisional,
            "base_threshold": base,
            "second_threshold": second,
        },
        taxable,
    )
    return _money(taxable)


def _compute_agi(ret: Return, half_se_tax: Decimal, sch_e_net: Decimal, rules: Rules, rec: _StepRecorder) -> tuple[Decimal, Decimal]:
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

    gross_excl_ss = (
        ret.wages
        + ret.interest_income + ret.k1_interest
        + ret.ordinary_dividends + ret.k1_ordinary_dividends
        + net_cap
        + ret.se_income
        + ret.other_ordinary_income
        + sch_e_net
        + ret.pension_distributions_taxable
        + ret.ira_distributions_taxable
    )
    # §86 — Social Security taxability depends on the rest of gross income.
    taxable_ss = _compute_taxable_ss(ret, gross_excl_ss, rules, rec)
    gross = gross_excl_ss + taxable_ss
    rec.add(
        "Gross income",
        "wages + interest(+k1) + ord_div(+k1) + net_cap + se + sch_e + other"
        " + pension + ira + taxable_ss",
        {
            "wages": ret.wages,
            "interest": ret.interest_income, "k1_interest": ret.k1_interest,
            "ord_div": ret.ordinary_dividends, "k1_ord_div": ret.k1_ordinary_dividends,
            "net_cap": net_cap,
            "se": ret.se_income,
            "sch_e": sch_e_net,
            "other": ret.other_ordinary_income,
            "pension_taxable": ret.pension_distributions_taxable,
            "ira_taxable": ret.ira_distributions_taxable,
            "taxable_ss": taxable_ss,
        },
        gross,
    )
    # Stash for TaxResult so the UI / math view can show retirement-income breakdown.
    rec._taxable_ss = taxable_ss  # type: ignore[attr-defined]

    # ── Traditional IRA deduction (§219) ──────────────────────────────────
    # Step A: compute "MAGI for IRA" = AGI as it would be WITHOUT the IRA
    # deduction. Per Form 1040 / Pub 590-A worksheet, MAGI for IRA is AGI
    # (computed without the IRA deduction) plus a few add-backs we don't
    # model (student-loan interest, foreign earned income exclusion, etc.).
    # We approximate MAGI ≈ gross − (all other adjustments).
    other_adjustments_total = (
        ret.hsa_deduction + ret.other_adjustments + half_se_tax
    )
    magi_for_ira = gross - other_adjustments_total
    ira_deduction_allowed, ira_deduction_disallowed = _compute_ira_deduction(
        ret, magi_for_ira, rules, rec
    )
    rec._ira_deduction_allowed = ira_deduction_allowed  # type: ignore[attr-defined]
    rec._ira_deduction_disallowed = ira_deduction_disallowed  # type: ignore[attr-defined]

    adjustments = (
        ret.hsa_deduction
        + ira_deduction_allowed
        + ret.other_adjustments
        + half_se_tax
    )
    rec.add(
        "Above-the-line adjustments",
        "hsa + trad_ira_deductible + other + ½ se_tax",
        {
            "hsa": ret.hsa_deduction,
            "trad_ira_deductible": ira_deduction_allowed,
            "other": ret.other_adjustments,
            "half_se_tax": half_se_tax,
        },
        adjustments,
    )
    agi = gross - adjustments
    rec.add("AGI", "gross − adjustments", {"gross": gross, "adjustments": adjustments}, agi)
    return agi, carryforward_out


def _compute_ira_deduction(
    ret: Return, magi: Decimal, rules: Rules, rec: _StepRecorder
) -> tuple[Decimal, Decimal]:
    """Returns (allowed_deduction, disallowed_portion).

    The disallowed portion is what would-be-deducted but is phased out under
    §219(g); economically it becomes nondeductible basis in the IRA.
    """
    contribution = ret.traditional_ira_contributions
    if contribution <= 0:
        return ZERO, ZERO

    cfg = rules.ira_deduction
    if cfg is None:
        # Legacy behavior: contributions deductible in full (no phaseout enforced).
        return _money(contribution), ZERO

    # 1. Annual contribution limit (with 50+ catch-up).
    limits = cfg.get("contribution_limit") or {}
    age = ret.taxpayer_age
    if age is not None and age >= 50:
        limit = Decimal(str(limits.get("fifty_plus", limits.get("under_50", 0))))
    else:
        limit = Decimal(str(limits.get("under_50", 0)))
    capped = min(contribution, limit) if limit > 0 else contribution

    # 2. Active-participant phaseout (§219(g)).
    status = _status(ret)
    if ret.is_covered_by_workplace_plan:
        ph = (cfg.get("phaseout_covered") or {}).get(status)
    elif ret.spouse_covered_by_workplace_plan and status == "mfj":
        ph = (cfg.get("phaseout_spouse_covered_only") or {}).get(status)
    elif ret.spouse_covered_by_workplace_plan and status == "mfs":
        # MFS living with spouse: $0–$10k window applies regardless of who is covered.
        ph = (cfg.get("phaseout_spouse_covered_only") or {}).get(status) \
            or (cfg.get("phaseout_covered") or {}).get(status)
    else:
        ph = None  # Not covered → full deduction up to limit.

    if ph is None:
        allowed = capped
    else:
        start = Decimal(str(ph.get("start", 0)))
        end = Decimal(str(ph.get("end", 0)))
        if magi <= start:
            allowed = capped
        elif magi >= end:
            allowed = ZERO
        else:
            # Linear ramp; round UP to nearest $10 per Pub 590-A, with $200 floor.
            ratio = (end - magi) / (end - start)
            allowed_raw = capped * ratio
            allowed = max(allowed_raw, Decimal("200")) if allowed_raw > 0 else ZERO
            allowed = min(allowed, capped)

    allowed = _money(allowed)
    disallowed = _money(capped - allowed)

    rec.add(
        "Traditional IRA deduction (§219)",
        "min(contribution, limit) reduced by active-participant phaseout vs MAGI",
        {
            "contribution": contribution,
            "limit": limit,
            "magi": magi,
            "covered_self": ret.is_covered_by_workplace_plan,
            "covered_spouse": ret.spouse_covered_by_workplace_plan,
        },
        allowed,
    )
    return allowed, disallowed


def _compute_taxable_income(
    ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder
) -> tuple[Decimal, Decimal, str, Decimal]:
    """Returns (taxable_income, deduction_used, deduction_kind, charitable_carryover_out).

    Charitable carryover (§170(d)): excess cash contributions over 60% AGI when
    itemizing carry forward up to 5 years."""
    std = rules.standard_deduction[_status(ret)]
    charitable_carry_out = ZERO
    pease_reduction = ZERO
    if ret.itemized_deductions is not None and ret.itemized_deductions > std:
        # Layer in prior-year charitable carryover, capped at 60% of AGI for cash gifts.
        carry_in = ret.charitable_carryover_in or ZERO
        cash_cap = agi * Decimal("0.60")
        # Approximate: assume ret.itemized_deductions already includes current-year
        # charitable. The carryover stacks on top; excess (over cap) re-carries.
        itemized_with_carry = ret.itemized_deductions + carry_in
        # The carryover only adds value up to the cap; anything beyond cap carries again.
        # Simplified: if current charitable + carry_in exceeds 60% AGI cash cap, the
        # excess (over the cap, ignoring non-cash mix) becomes new carryover.
        current_charitable = ret.charitable_contributions or ZERO
        total_charitable = current_charitable + carry_in
        if total_charitable > cash_cap:
            used_charitable = cash_cap
            charitable_carry_out = total_charitable - cash_cap
            # Replace excess charitable contribution in itemized with the cap.
            itemized_used = ret.itemized_deductions - current_charitable + used_charitable
        else:
            itemized_used = itemized_with_carry
        if carry_in > 0:
            rec.add(
                "Charitable carryover applied",
                "prior carryover stacked into itemized (60% AGI cash cap)",
                {"carry_in": carry_in, "cash_cap_60pct_agi": cash_cap,
                 "carry_out": charitable_carry_out},
                itemized_used,
            )
        # Pease limitation (pre-TCJA): cut itemized deductions by 3% of AGI above
        # threshold, capped at 80% reduction. Excludes medical, investment interest,
        # casualty, gambling losses — we approximate against the full itemized total.
        if rules.pease:
            ptab = rules.pease
            pthr = Decimal(ptab["threshold"][_status(ret)])
            if agi > pthr:
                prate = Decimal(str(ptab.get("rate", "0.03")))
                pmax = Decimal(str(ptab.get("max_reduction", "0.80")))
                gross_cut = (agi - pthr) * prate
                pease_reduction = min(gross_cut, itemized_used * pmax).quantize(Decimal("0.01"))
                itemized_used = itemized_used - pease_reduction
                rec.add(
                    "Pease limitation on itemized",
                    "min(3% × (AGI − threshold), 80% × itemized)",
                    {"threshold": pthr, "agi": agi, "reduction": pease_reduction},
                    itemized_used,
                )
        if itemized_used > std:
            deduction = itemized_used
            kind = "itemized"
        else:
            deduction = std
            kind = "standard"
            # If we fell back to standard, the carryover wasn't actually used.
            charitable_carry_out = (ret.charitable_carryover_in or ZERO)
            pease_reduction = ZERO
    else:
        deduction = std
        kind = "standard"
        # Standard-deduction year: any prior-year charitable carryover survives.
        charitable_carry_out = ret.charitable_carryover_in or ZERO
    rec.add(
        f"{kind.capitalize()} deduction",
        f"{kind} ({_status(ret).upper()}, {ret.tax_year})",
        {"kind": kind, "amount": deduction, "standard": std, "itemized": ret.itemized_deductions},
        deduction,
    )
    # Personal exemption (TY2017 and earlier). Subtract amount × (1 + spouse + dependents).
    # PEP phaseout: amount fully phased out by phaseout_complete, partial in between.
    pe_used = ZERO
    if rules.personal_exemption:
        pe = rules.personal_exemption
        per = Decimal(pe["amount"])
        spouse = 1 if ret.filing_status in (FilingStatus.MFJ, FilingStatus.QSS) else 0
        deps = ret.qualifying_children + ret.other_dependents
        count = 1 + spouse + deps
        raw_pe = per * count
        # PEP: 2% reduction per $2,500 (or fraction) over threshold, fully phased
        # out at threshold + $122,500 (single) / etc — supplied via phaseout_complete.
        scale = Decimal("1.0")
        pstart_tab = pe.get("phaseout_start")
        pcomp_tab = pe.get("phaseout_complete")
        if pstart_tab and pcomp_tab:
            pstart = Decimal(pstart_tab[_status(ret)])
            pcomp = Decimal(pcomp_tab[_status(ret)])
            if agi >= pcomp:
                scale = Decimal("0")
            elif agi > pstart:
                # 2% per $2,500 (or fraction). Step function.
                steps = ((agi - pstart) / Decimal("2500")).to_integral_value(rounding="ROUND_CEILING")
                scale = max(Decimal("0"), Decimal("1") - steps * Decimal("0.02"))
        pe_used = (raw_pe * scale).quantize(Decimal("0.01"))
        rec.add(
            "Personal exemption",
            "amount × (1 + spouse + dependents) × PEP_scale",
            {"per": per, "count": count, "raw": raw_pe, "scale": scale},
            pe_used,
        )
    taxable = max(ZERO, agi - deduction - pe_used)

    # NOL §172: post-TCJA, NOL offsets up to 80% of pre-NOL taxable income.
    # Pre-TCJA (rules.nol_full_offset = True): can fully offset taxable income.
    # Excess NOL carries forward indefinitely.
    nol_in = ret.nol_carryforward_in or ZERO
    nol_used = ZERO
    nol_out = ZERO
    if nol_in > 0 and taxable > 0:
        if rules.nol_full_offset:
            cap = taxable
        else:
            cap = (taxable * Decimal("0.80")).quantize(Decimal("0.01"))
        nol_used = min(nol_in, cap)
        nol_out = nol_in - nol_used
        taxable = taxable - nol_used
        rec.add(
            "NOL §172 applied",
            "min(nol_in, cap × taxable); excess carries forward",
            {"nol_in": nol_in, "cap": cap, "nol_used": nol_used, "nol_out": nol_out,
             "full_offset_pre_tcja": rules.nol_full_offset},
            taxable,
        )
    elif nol_in > 0:
        # Taxable income is already 0; entire NOL carries forward.
        nol_out = nol_in

    rec.add(
        "Taxable income",
        "max(0, agi − deduction − personal_exemption − nol_used)",
        {"agi": agi, "deduction": deduction, "personal_exemption": pe_used, "nol_used": nol_used},
        taxable,
    )
    # Stash nol_out + charitable_carry_out + pe + pease on the recorder so
    # compute() can pluck them without changing the return tuple shape.
    rec._nol_out = nol_out  # type: ignore[attr-defined]
    rec._charitable_out = charitable_carry_out  # type: ignore[attr-defined]
    rec._pe_used = pe_used  # type: ignore[attr-defined]
    rec._pease_reduction = pease_reduction  # type: ignore[attr-defined]
    return taxable, deduction, kind, nol_out


def _compute_ftc(ret: Return, regular_tax: Decimal, agi: Decimal,
                  rec: _StepRecorder) -> tuple[Decimal, Decimal]:
    """Foreign Tax Credit §901/§904. Returns (ftc_used, ftc_carry_out).

    Simplified §904 limitation: FTC capped at (foreign income / total income) ×
    pre-FTC regular tax. We don't have foreign-income tracked separately, so
    we approximate the limit as the smaller of (foreign_taxes_paid + carry_in)
    and the regular tax itself. Excess carries forward 10 years.
    """
    available = (ret.foreign_taxes_paid or ZERO) + (ret.ftc_carryforward_in or ZERO)
    if available <= 0:
        return ZERO, ZERO
    # Limit at regular tax (simplified §904).
    used = min(available, max(regular_tax, ZERO))
    carry_out = available - used
    rec.add(
        "Foreign Tax Credit §901/§904",
        "min(foreign_taxes + carry_in, regular_tax); excess carries 10y",
        {"foreign_taxes_paid": ret.foreign_taxes_paid,
         "carry_in": ret.ftc_carryforward_in, "limit": regular_tax,
         "used": used, "carry_out": carry_out},
        used,
    )
    return used, carry_out


def _compute_amt_credit(ret: Return, regular_tax: Decimal, amt: Decimal,
                         rec: _StepRecorder) -> tuple[Decimal, Decimal]:
    """Form 8801 Minimum Tax Credit. Returns (credit_used, new_carry_out).

    Simplified: prior-year AMT (from timing items like ISO exercises) generates
    a credit usable only in years where regular tax exceeds tentative minimum
    tax. We approximate "regular > AMT" by checking that this year's AMT == 0.
    The current year's AMT (if any) is added to the carryforward going out.
    """
    carry_in = ret.amt_credit_carryforward_in or ZERO
    used = ZERO
    if carry_in > 0 and amt == 0 and regular_tax > 0:
        # Usable up to (regular_tax - 0) = regular_tax.
        used = min(carry_in, regular_tax)
    # Carry-out = remaining prior + any new AMT generated this year.
    carry_out = (carry_in - used) + amt
    if used > 0 or carry_out > 0:
        rec.add(
            "AMT credit (Form 8801)",
            "use prior MTC when AMT=0; this year's AMT adds to carryforward",
            {"carry_in": carry_in, "used": used, "amt_this_year": amt,
             "carry_out": carry_out},
            used,
        )
    return used, carry_out


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


def _compute_ctc(ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder) -> tuple[Decimal, Decimal]:
    """Returns (total_ctc_after_phaseout, ctc_kid_portion_after_phaseout).

    The kid portion is reported separately so the ACTC (refundable Additional
    CTC) can use it as its $1,700/kid refundability ceiling (Form 8812)."""
    cfg = rules.ctc
    if ret.qualifying_children <= 0 and ret.other_dependents <= 0:
        return ZERO, ZERO
    per_child = Decimal(cfg["per_qualifying_child"])
    per_odc   = Decimal(cfg.get("per_other_dependent", 500))
    raw_ctc = per_child * ret.qualifying_children
    raw_odc = per_odc * ret.other_dependents
    raw = raw_ctc + raw_odc
    threshold = Decimal(cfg["phaseout_start"][_status(ret)])
    if agi > threshold:
        over = ((agi - threshold) / 1000).to_integral_value(rounding="ROUND_CEILING")
        reduction = over * Decimal(cfg["phaseout_per_1000_agi"])
        credit = max(ZERO, raw - reduction)
        # Reduce the kid portion first per IRS ordering (ODC reduced last).
        kid_after = max(ZERO, raw_ctc - reduction)
    else:
        reduction = ZERO
        credit = raw
        kid_after = raw_ctc
    rec.add(
        "Child Tax Credit + ODC",
        "(CTC × children + ODC × other_deps) − phaseout",
        {
            "per_child": per_child,
            "children": ret.qualifying_children,
            "per_other_dependent": per_odc,
            "other_dependents": ret.other_dependents,
            "raw_ctc": raw_ctc, "raw_odc": raw_odc, "raw": raw,
            "agi": agi, "threshold": threshold, "reduction": reduction,
        },
        credit,
    )
    return _money(credit), _money(kid_after)


def _compute_savers_credit(ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder) -> Decimal:
    """Form 8880 — Retirement Savings Contributions Credit. Nonrefundable.
    Credit rate (50/20/10%) drops in steps as AGI rises. Max $2,000 of
    contributions counted per person (so up to $1k/$2k credit)."""
    cfg = rules.savers_credit
    if not cfg:
        return ZERO
    contribs = (
        max(ZERO, ret.traditional_401k_contributions)
        + max(ZERO, ret.roth_401k_contributions)
        + max(ZERO, ret.traditional_ira_contributions)
        + max(ZERO, ret.roth_ira_contributions)
    )
    if contribs <= ZERO:
        return ZERO
    n_persons = 2 if ret.filing_status in (FilingStatus.MFJ, FilingStatus.QSS) else 1
    cap = Decimal(str(cfg["max_contribution_per_person"])) * n_persons
    capped = min(contribs, cap)
    rates = cfg["rates"][_status(ret)]
    rate = ZERO
    for agi_limit, r in rates:
        if agi <= Decimal(str(agi_limit)):
            rate = Decimal(str(r))
            break
    credit = capped * rate
    if credit > ZERO:
        rec.add(
            "Saver's Credit (Form 8880) — nonrefundable",
            "min(contribs, $2k × persons) × tier-rate(AGI)",
            {"contribs": contribs, "capped": capped, "rate": rate, "agi": agi},
            credit,
        )
    return _money(credit)


def _compute_ptc(ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder) -> tuple[Decimal, Decimal]:
    """Form 8962 — Premium Tax Credit. Returns (net_ptc, excess_aptc_repayment).

      - net_ptc > 0 means a refundable credit (PTC > APTC)
      - excess_aptc_repayment > 0 means additional tax owed (PTC < APTC),
        capped per FPL bucket below 400%.
    """
    cfg = rules.ptc
    if not cfg or ret.marketplace_household_size <= 0:
        return ZERO, ZERO
    if ret.marketplace_slcsp_annual <= ZERO:
        return ZERO, ZERO

    base = Decimal(str(cfg["fpl_base"]))
    increment = Decimal(str(cfg["fpl_increment_per_person"]))
    fpl = base + increment * Decimal(ret.marketplace_household_size - 1)
    if fpl <= ZERO:
        return ZERO, ZERO

    pct_fpl = (agi / fpl) * Decimal(100)

    # Piecewise-linear applicable figure curve.
    applicable_figure = Decimal(str(cfg["applicable_figure"][-1][3]))  # default = last rate
    for low, high, r_low, r_high in cfg["applicable_figure"]:
        low_d, high_d = Decimal(str(low)), Decimal(str(high))
        r_low_d, r_high_d = Decimal(str(r_low)), Decimal(str(r_high))
        if low_d <= pct_fpl < high_d:
            span = high_d - low_d
            applicable_figure = (
                r_low_d if span == ZERO
                else r_low_d + (r_high_d - r_low_d) * ((pct_fpl - low_d) / span)
            )
            break

    annual_contrib = agi * applicable_figure
    ptc_before_cap = max(ZERO, ret.marketplace_slcsp_annual - annual_contrib)
    ptc = min(ptc_before_cap, ret.marketplace_plan_premium_annual or ret.marketplace_slcsp_annual)

    aptc = ret.marketplace_advance_ptc_paid
    if ptc >= aptc:
        net_ptc = ptc - aptc
        repayment = ZERO
    else:
        net_ptc = ZERO
        excess = aptc - ptc
        is_family = ret.marketplace_household_size >= 2
        cap = None
        for bucket in cfg["repayment_limits"]:
            if pct_fpl < Decimal(str(bucket["below_pct_fpl"])):
                cap = Decimal(str(bucket["family" if is_family else "single"]))
                break
        repayment = excess if cap is None else min(excess, cap)

    rec.add(
        "Premium Tax Credit (Form 8962)",
        "PTC = min(SLCSP − household_income × applicable_figure(pct_FPL), plan_premium); reconcile vs APTC",
        {
            "household_size": ret.marketplace_household_size,
            "fpl": fpl, "pct_fpl": pct_fpl,
            "applicable_figure": applicable_figure,
            "annual_contrib": annual_contrib,
            "slcsp": ret.marketplace_slcsp_annual,
            "computed_ptc": ptc, "aptc": aptc,
            "net_ptc": net_ptc, "repayment": repayment,
        },
        net_ptc - repayment,
    )
    return _money(net_ptc), _money(repayment)


def _compute_eitc(ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder) -> Decimal:
    """Earned Income Tax Credit (Schedule EIC). Refundable.

    Form is a trapezoid per #-of-children:
      - Phase-in:   credit = earned × phase_in_rate, up to max_credit
      - Plateau:    credit = max_credit
      - Phase-out:  credit = max_credit − (income_for_phaseout − ph_start) × ph_rate

    Phase-out is applied against the **greater of** earned income or AGI
    (IRS rule: this prevents gaming the credit with investment income).
    Disqualifiers: filing MFS, or investment income above the annual limit.
    """
    cfg = rules.eitc
    if not cfg:
        return ZERO
    if ret.filing_status == FilingStatus.MFS:
        return ZERO
    earned = max(ZERO, ret.wages) + max(ZERO, ret.se_income)
    investment = (
        max(ZERO, ret.interest_income)
        + max(ZERO, ret.ordinary_dividends)
        + max(ZERO, ret.long_term_capital_gains)
        + max(ZERO, ret.short_term_capital_gains)
        + max(ZERO, ret.royalty_income)
    )
    inv_limit = Decimal(cfg["investment_income_limit"])
    if investment > inv_limit:
        rec.add("EITC disallowed — investment income over limit",
                "investment_income > eitc.investment_income_limit",
                {"investment_income": investment, "limit": inv_limit}, ZERO)
        return ZERO
    if earned <= ZERO:
        return ZERO

    n = min(max(0, ret.qualifying_children), 3)
    params = cfg["parameters"][str(n)]
    joint = ret.filing_status in (FilingStatus.MFJ, FilingStatus.QSS)
    earned_inc_amount = Decimal(str(params["earned_income_amount"]))
    max_credit = Decimal(str(params["max_credit"]))
    ph_rate = Decimal(str(params["phaseout_rate"]))
    ph_start = Decimal(str(params["phaseout_start_joint" if joint else "phaseout_start"]))
    comp = Decimal(str(params["completed_phaseout_joint" if joint else "completed_phaseout"]))

    # Phase-in (linear): tentative credit grows with earned income.
    phase_in_rate = max_credit / earned_inc_amount
    tentative = min(earned * phase_in_rate, max_credit)

    # Phase-out: against greater of earned or AGI.
    income_for_phaseout = max(earned, agi)
    if income_for_phaseout >= comp:
        credit = ZERO
    elif income_for_phaseout > ph_start:
        phaseout_reduction = (income_for_phaseout - ph_start) * ph_rate
        credit = min(tentative, max(ZERO, max_credit - phaseout_reduction))
    else:
        credit = tentative

    rec.add(
        "Earned Income Tax Credit (Schedule EIC) — refundable",
        "trapezoid by # qualifying children; phase-out uses max(earned, AGI)",
        {
            "qualifying_children": n,
            "earned_income": earned,
            "investment_income": investment,
            "phase_in_rate": phase_in_rate,
            "max_credit": max_credit,
            "phaseout_start": ph_start,
            "completed_phaseout": comp,
            "income_for_phaseout": income_for_phaseout,
        },
        credit,
    )
    return _money(credit)


def _compute_education_credits(
    ret: Return, agi: Decimal, rules: Rules, rec: _StepRecorder
) -> tuple[Decimal, Decimal, Decimal]:
    """Form 8863 — AOTC + LLC.

    Returns (aotc_nonrefundable, aotc_refundable, llc_nonrefundable). MFS is
    disallowed for both. Both share the same MAGI phaseout window.
    """
    cfg = rules.education_credits
    if not cfg:
        return ZERO, ZERO, ZERO
    if ret.filing_status == FilingStatus.MFS:
        return ZERO, ZERO, ZERO

    ph = cfg["phaseout"][_status(ret)]
    ph_start = Decimal(str(ph[0]))
    ph_end = Decimal(str(ph[1]))
    if ph_end <= ZERO:
        return ZERO, ZERO, ZERO
    if agi >= ph_end:
        phase_factor = ZERO
    elif agi <= ph_start:
        phase_factor = Decimal(1)
    else:
        phase_factor = (ph_end - agi) / (ph_end - ph_start)

    # ── AOTC ── per qualifying student, up to 4 (Form 8863 part 1)
    aotc_cfg = cfg["aotc"]
    max_exp = Decimal(str(aotc_cfg["max_expenses_per_student"]))
    first_cap = Decimal(str(aotc_cfg["first_tier_cap"]))
    first_rate = Decimal(str(aotc_cfg["first_tier_rate"]))
    second_rate = Decimal(str(aotc_cfg["second_tier_rate"]))
    refund_frac = Decimal(str(aotc_cfg["refundable_fraction"]))
    aotc_raw = ZERO
    for expenses in (ret.aotc_qualified_expenses or [])[:4]:
        e = min(max(ZERO, expenses), max_exp)
        first_tier = min(e, first_cap) * first_rate
        second_tier = max(ZERO, e - first_cap) * second_rate
        aotc_raw += first_tier + second_tier
    aotc_total = aotc_raw * phase_factor
    aotc_refundable = aotc_total * refund_frac
    aotc_nonrefundable = aotc_total - aotc_refundable

    # ── LLC ── single bucket per return (Form 8863 part 2)
    llc_cfg = cfg["llc"]
    llc_cap = Decimal(str(llc_cfg["expense_cap"]))
    llc_rate = Decimal(str(llc_cfg["rate"]))
    llc_raw = min(max(ZERO, ret.llc_qualified_expenses), llc_cap) * llc_rate
    llc_credit = llc_raw * phase_factor

    if aotc_raw + llc_raw > ZERO:
        rec.add(
            "Education credits (Form 8863) — AOTC + LLC",
            "per-student AOTC (100% / 25% / 40% refundable) + LLC (20% of cap) × MAGI phaseout",
            {
                "aotc_students": len(ret.aotc_qualified_expenses or []),
                "aotc_raw": aotc_raw,
                "llc_expenses": ret.llc_qualified_expenses,
                "llc_raw": llc_raw,
                "phase_factor": phase_factor,
                "phaseout_start": ph_start, "phaseout_end": ph_end,
            },
            aotc_total + llc_credit,
        )
    return _money(aotc_nonrefundable), _money(aotc_refundable), _money(llc_credit)


# ────────────────────────── public entry point ──────────────────────────

def compute(ret: Return, rules: Rules | None = None) -> TaxResult:
    """Run the full federal tax computation for one return."""
    rules = rules or load_rules(ret.tax_year)
    if rules.year != ret.tax_year:
        raise ValueError(f"rules year {rules.year} ≠ return year {ret.tax_year}")

    rec = _StepRecorder()

    # ── Schedule E MACRS depreciation (Form 4562) ──
    # Compute per-property MACRS depreciation and any disposition gain/recapture,
    # then fold the results into an "effective" Return that downstream stages see.
    from .depreciation import compute_all as _dep_compute_all
    prop_results = _dep_compute_all(ret.rental_properties, ret.tax_year)
    total_depreciation = sum((p.current_year_deduction for p in prop_results), ZERO)
    total_recapture_1250 = sum((p.sale_recapture_1250 for p in prop_results), ZERO)
    total_excess_ltcg = sum(
        (max(ZERO, p.sale_total_gain - p.sale_recapture_1250) for p in prop_results), ZERO
    )
    accumulated_map = {p.property_id: p.accumulated_after for p in prop_results}

    if prop_results and (total_depreciation or total_recapture_1250 or total_excess_ltcg):
        rec.add(
            "MACRS depreciation (Form 4562) + §1250 recapture on disposition",
            "Σ per-property mid-month SL deduction; sale gain → unrecaptured §1250 (25%) + LTCG excess",
            {
                "properties": len(ret.rental_properties),
                "current_year_dep": total_depreciation,
                "recapture_1250": total_recapture_1250,
                "ltcg_excess": total_excess_ltcg,
            },
            total_depreciation,
        )

    if total_depreciation or total_recapture_1250 or total_excess_ltcg:
        ret = ret.model_copy(update={
            "rental_net_income": ret.rental_net_income - total_depreciation,
            "unrecaptured_1250_gains": ret.unrecaptured_1250_gains + total_recapture_1250,
            "long_term_capital_gains": ret.long_term_capital_gains + total_excess_ltcg,
        })

    se_tax, half_se_tax = _compute_se_tax(ret, rules, rec)
    sch_e_net, pal_carry, _ = _compute_schedule_e(ret, rec)
    agi, capital_loss_carry_out = _compute_agi(ret, half_se_tax, sch_e_net, rules, rec)
    taxable_pre_qbi, deduction, deduction_kind, nol_carry_out = _compute_taxable_income(ret, agi, rules, rec)
    charitable_carry_out = getattr(rec, "_charitable_out", ZERO)
    pe_used = getattr(rec, "_pe_used", ZERO)
    pease_reduction = getattr(rec, "_pease_reduction", ZERO)
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
    ctc, ctc_kid_after = _compute_ctc(ret, agi, rules, rec)
    eitc = _compute_eitc(ret, agi, rules, rec)
    aotc_nonref, aotc_ref, llc = _compute_education_credits(ret, agi, rules, rec)
    savers = _compute_savers_credit(ret, agi, rules, rec)
    net_ptc, aptc_repayment = _compute_ptc(ret, agi, rules, rec)
    ftc_used, ftc_carry_out = _compute_ftc(ret, pre_amt_regular, agi, rec)
    amt_credit_used, amt_credit_carry_out = _compute_amt_credit(ret, pre_amt_regular, amt, rec)

    # ── ACTC (Form 8812) — refundable portion of CTC ──
    # Compute tax available to absorb the nonrefundable CTC: it stacks after
    # other nonrefundable credits but before ACTC.
    other_nonref = ftc_used + amt_credit_used + aotc_nonref + llc + savers
    tax_after_other_nonref = max(ZERO, pre_amt_regular + amt - other_nonref)
    ctc_nonref_used = min(ctc, tax_after_other_nonref)
    ctc_leftover = ctc - ctc_nonref_used
    actc_kid_cap = Decimal(rules.ctc.get("refundable_per_child", 1700)) * ret.qualifying_children
    earned = max(ZERO, ret.wages) + max(ZERO, ret.se_income)
    actc_earned_threshold = Decimal(rules.ctc.get("actc_earned_threshold", 2500))
    actc_rate = Decimal(str(rules.ctc.get("actc_rate", "0.15")))
    earnings_test = max(ZERO, (earned - actc_earned_threshold) * actc_rate)
    if rules.ctc.get("actc_full_refund", False):
        # ARPA 2021: fully refundable, no earnings test, no per-kid cap.
        actc = ctc_leftover
    elif rules.ctc.get("actc_no_kid_cap", False):
        # Pre-TCJA: 15% × (earned − $3k), no per-kid cap.
        actc = min(ctc_leftover, earnings_test)
    else:
        actc = min(ctc_leftover, actc_kid_cap, earnings_test)
    if actc > ZERO:
        rec.add(
            "Additional Child Tax Credit (Form 8812) — refundable",
            "min(unused CTC, $1,700 × kids, 15% × (earned − $2,500))",
            {"ctc_leftover": ctc_leftover, "kid_cap": actc_kid_cap,
             "earnings_test": earnings_test, "earned": earned},
            actc,
        )

    credits = ctc_nonref_used + ftc_used + amt_credit_used + aotc_nonref + llc + savers

    # §72(t) — 10% additional tax on early (pre-59½) retirement-plan distributions.
    ewp_rate = rules.early_withdrawal_penalty_rate
    early_withdrawal_penalty = _money(
        max(ZERO, ret.early_withdrawal_subject_to_penalty) * ewp_rate
    )
    if early_withdrawal_penalty > 0:
        rec.add(
            "Early withdrawal penalty (§72(t) / Form 5329)",
            f"taxable_early_distribution × {ewp_rate}",
            {"taxable_early_distribution": ret.early_withdrawal_subject_to_penalty,
             "rate": ewp_rate},
            early_withdrawal_penalty,
        )

    total_tax = (pre_amt_regular + amt + se_tax + addl_medicare + niit
                 + aptc_repayment + early_withdrawal_penalty) - credits
    total_tax = max(ZERO, total_tax)
    rec.add(
        "Total tax",
        "ordinary + qualified + coll + 1250 + amt + se + addl_medicare + niit"
        " + APTC_repay + early_wd_penalty − credits",
        {
            "ordinary": ord_tax, "qualified": qual_tax,
            "collectibles": coll_tax, "unrecaptured_1250": unrec_tax,
            "amt": amt, "se": se_tax, "addl_medicare": addl_medicare,
            "niit": niit, "aptc_repayment": aptc_repayment,
            "early_wd_penalty": early_withdrawal_penalty,
            "credits": credits,
        },
        total_tax,
    )

    # Refundable credits are treated like payments on Form 1040.
    payments = (ret.federal_withholding + ret.estimated_payments
                + eitc + aotc_ref + actc + net_ptc)
    refund = payments - total_tax
    rec.add(
        "Refund (+) / owed (−)",
        "withholding + estimated + EITC + AOTC_refundable + ACTC + net_PTC − total_tax",
        {"withholding": ret.federal_withholding, "estimated": ret.estimated_payments,
         "eitc": eitc, "aotc_refundable": aotc_ref, "actc": actc, "net_ptc": net_ptc,
         "total_tax": total_tax},
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
        depreciation_current_year=_money(total_depreciation),
        depreciation_accumulated_out=accumulated_map,
        eitc=eitc,
        aotc_nonrefundable=aotc_nonref,
        aotc_refundable=aotc_ref,
        llc_credit=llc,
        savers_credit=savers,
        actc=actc,
        ptc_net=net_ptc,
        ptc_excess_aptc_repayment=aptc_repayment,
        personal_exemption_used=_money(pe_used),
        pease_reduction=_money(pease_reduction),
        social_security_taxable=_money(getattr(rec, "_taxable_ss", ZERO)),
        pension_taxable=_money(ret.pension_distributions_taxable),
        ira_taxable=_money(ret.ira_distributions_taxable),
        early_withdrawal_penalty=early_withdrawal_penalty,
        ira_deduction_allowed=_money(getattr(rec, "_ira_deduction_allowed", ZERO)),
        ira_deduction_disallowed=_money(getattr(rec, "_ira_deduction_disallowed", ZERO)),
        capital_loss_carryforward_out=_money(capital_loss_carry_out),
        nol_carryforward_out=_money(nol_carry_out),
        amt_credit_carryforward_out=_money(amt_credit_carry_out),
        ftc_carryforward_out=_money(ftc_carry_out),
        charitable_carryover_out=_money(charitable_carry_out),
        reported_total_tax=ret.reported_total_tax,
        reconciliation_delta=delta,
    )
